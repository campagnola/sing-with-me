import sys, threading, atexit, queue, socket, time, argparse, struct
from typing import Optional, NamedTuple, Tuple
import numpy as np
import pyaudio


class AudioChunk(NamedTuple):
    data: np.ndarray
    pointer: int
    timestamp: float


class InputStream:
    def __init__(self):
        self.connections = []

    def connect(self, target):
        """Add new frame callback
        """
        self.connections.append(target)

    def send(self, data):
        for target in self.connections:
            target.add_buffer(data)


class RecordingStream(InputStream):
    def __init__(self, sample_rate, frames_per_buffer):
        InputStream.__init__(self)

        self.pointer = 0

        self.in_stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            stream_callback=self.new_frame,
            frames_per_buffer=frames_per_buffer,
        )

    def stop(self):
        self.in_stream.stop_stream()
        self.in_stream.close()

    def new_frame(self, data, n_frames, time_info, status):
        arr = np.frombuffer(data, dtype='int16')

        d = AudioChunk(arr, self.pointer, time.time())
        self.send(d)
        
        self.pointer += len(arr)
        return None, pyaudio.paContinue


class PlayHead(NamedTuple):
    pointer: int
    timestamp: float


class PlaybackStream:
    ring_buffer: np.ndarray
    play_head: PlayHead

    def __init__(self, sample_rate:float, frames_per_buffer:int):
        self.out_stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            frames_per_buffer=frames_per_buffer,
            output=True,
        )

        self.sample_rate = sample_rate
        self.frames_per_buffer = frames_per_buffer
        self.buffer_duration = frames_per_buffer / sample_rate

        self.ring_buffer = np.zeros(frames_per_buffer * int(10.0 / self.buffer_duration), dtype='int16')
        self.play_head = PlayHead(0, 0)

        self.running = True

        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        self.start_time = start_time = time.time()
        ring_buffer = self.ring_buffer
        frames_per_buffer = self.frames_per_buffer
        sample_rate = self.sample_rate

        # pre-load so there is always at least 1 buffer of extra data queued
        self.out_stream.write(b'\0\0' * 512)

        while self.running:
            read_pointer = self.play_head.pointer
            ring_ptr = read_pointer % len(ring_buffer)

            # sleep until time to play the next chunk
            now = time.time()
            next_play_time = start_time + read_pointer / sample_rate
            dt = (next_play_time - now) - 2e-3
            if dt > 0:
                time.sleep(dt)
            
            buf:np.ndarray = ring_buffer[ring_ptr:ring_ptr+frames_per_buffer]
            self.out_stream.write(buf.tobytes())
            ring_buffer[ring_ptr:ring_ptr+frames_per_buffer] = 0
            read_pointer += len(buf)
            self.play_head = PlayHead(read_pointer, now)

    def stop(self):
        self.running = False
        self.thread.join()
        self.out_stream.stop_stream()
        self.out_stream.close()

    def index_at_time(self, time:float):
        return int((time - self.start_time) * sample_rate)

    def add_buffer_data(self, data:np.ndarray, index:int):
        """add data into ring buffer, possibly in two parts if it wraps around to the beginning
        """
        ring_buffer = self.ring_buffer
        ring_ptr = index % len(ring_buffer)

        first_size = min(len(data), len(ring_buffer) - ring_ptr)
        ring_buffer[ring_ptr:ring_ptr+first_size] += data[:first_size]
        if first_size < len(data):
            second_size = len(data) - first_size
            ring_buffer[:second_size] = data[first_size:]


class StreamWriter:
    """Receive timestamped sound chunks, write into an output stream with a target latency
    """
    thread: Optional[threading.Thread]
    data_queue: queue.PriorityQueue

    def __init__(self, in_stream:InputStream, out_stream:PlaybackStream, latency:float=100e-3, thread:bool=False):
        self.in_stream = in_stream
        self.out_stream = out_stream
        self.latency = latency
        self.data_queue = queue.PriorityQueue()
        self.running = True

        if thread:
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
        else:
            self.thread = None

        self.index_offset:int = 0

        in_stream.connect(self)

    def add_buffer(self, data:AudioChunk):
        if not self.running:
            return
        if self.thread is None:
            self.handle_data(data)
        else:
            self.data_queue.put(data)

    def handle_data(self, data:AudioChunk):
        # print("Handle data")
        sample_rate = self.out_stream.sample_rate
        buf, index, play_time = data
        # print("   data:", index, play_time)

        last_play_index, last_play_time = self.out_stream.play_head
        # print("   play head:", last_play_index, last_play_time)
        desired_play_time = play_time + self.latency
        desired_play_index = last_play_index + int((desired_play_time - last_play_time) * sample_rate)
        # print("   desired play:", desired_play_index, desired_play_time)
        desired_index_offset = desired_play_index - index
        # print("   desired offset:", desired_index_offset)
        if abs(self.index_offset - desired_index_offset) > (10e-3 * sample_rate):
            # print("adjust index offset:", self.index_offset, desired_index_offset)
            self.index_offset = desired_index_offset

        write_pointer = index + self.index_offset
        # print("   write:", write_pointer, self.index_offset)

        self.out_stream.add_buffer_data(buf, write_pointer)

    def stop(self):
        self.running = False
        if self.thread is not None:
            self.thread.join()

    def run(self):
        while self.running:
            data = self.data_queue.get()
            self.handle_data(data)


class UDPStream(InputStream):
    remote_address: Optional[Tuple[str,int]]
    local_address: Tuple[str,int]

    @staticmethod
    def parse_address(addr:str) -> Tuple[str,int]:
        parts = addr.split(':')
        if len(parts) == 1:
            parts.append('31415')
        return (parts[0], int(parts[1]))

    def __init__(self, remote_address:Optional[str], local_address='0.0.0.0:31415'):
        InputStream.__init__(self)

        self.local_address = self.parse_address(local_address)
        self.running = True

        self.clock_offset = 0

        self.recv_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.recv_socket.bind(self.local_address)
        self.recv_socket.settimeout(1)

        self.send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        if remote_address is None:
            self.remote_address = None
        else:
            self.remote_address = self.parse_address(remote_address)
            self.measure_lag()

        self.recv_thread = threading.Thread(target=self.recv_loop, daemon=True)
        self.recv_thread.start()

    def measure_lag(self):
        assert self.remote_address is not None
        timing = []
        msg = b'p' + struct.pack('d', time.time())
        for i in range(100):
            now = time.time()
            self.send_socket.sendto(msg, self.remote_address)
            while True:
                try:
                    data, address = self.recv_socket.recvfrom(9)
                    if data[0:1] != b'r':
                        continue
                    after = time.time()
                    assert address[0] == self.remote_address[0]
                    then = struct.unpack('d', data[1:9])[0]
                    timing.append([now, then, after])
                    break
                except socket.timeout:
                    break

        timing = np.array(timing)
        avg_roundtrip = (timing[:,2] - timing[:,0]).mean()
        print("Received %d/100 poing replies; avg roundtrip = %0.2f ms" % (len(timing), avg_roundtrip * 1000))

        self.clock_offset = ((0.5 * (timing[:,2] + timing[:,0])) - timing[:,1]).mean()
        print("Clock offset:", self.clock_offset)

    def recv_loop(self):
        sock = self.recv_socket
        while self.running:
            try:
                data, address = sock.recvfrom(50000)
                if self.remote_address is None:
                    self.remote_address = (address[0], 31415)
                msg_type = chr(data[0])
                if msg_type == 'a':
                    index, timestamp = struct.unpack('Qd', data[1:17])
                    # audio data follows header
                    buf = np.frombuffer(data[17:], dtype='int16')
                    # send received data to other (local) listeners
                    self.send(AudioChunk(buf, index, timestamp + self.clock_offset))
                elif msg_type == 'p':
                    # ping
                    msg = b'r' + struct.pack('d', time.time())
                    self.send_socket.sendto(msg, self.remote_address)
                else:
                    print(data)
                    raise ValueError("Unrecognized message type: %r" % msg_type)

            except socket.timeout:
                continue

    def stop(self):
        self.running = False
        self.recv_thread.join()
        self.recv_socket.close()
        if self.send_socket is not None:
            self.send_socket.close()

    def add_buffer(self, data:AudioChunk):
        """Send the audio chunk to the remote host(s)
        """
        if self.remote_address is None:
            return
        msg = b'a' + struct.pack('id', data.pointer, data.timestamp) + data.data.tobytes()
        self.send_socket.sendto(msg, self.remote_address)


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('address', type=str, nargs='?', default=None, help='IP address:port to send data to')
    parser.add_argument('--listen', type=str, default='0.0.0.0:31415', help='IP address:port to listen for incoming data')
    args = parser.parse_args()

    pa = pyaudio.PyAudio()

    sample_rate = 22050
    latency = 50e-3

    mic_stream = RecordingStream(sample_rate, frames_per_buffer=128)
    out_stream = PlaybackStream(sample_rate, frames_per_buffer=128)
    mic_writer = StreamWriter(mic_stream, out_stream, latency=latency)

    udp_stream = UDPStream(args.address, args.listen)
    mic_stream.connect(udp_stream)
    udp_writer = StreamWriter(udp_stream, out_stream, latency=latency)

    def quit():
        out_stream.stop()
        mic_stream.stop()
        pa.terminate()

    atexit.register(quit)
