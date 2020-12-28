import sys, threading, atexit, queue, socket, time, argparse
from typing import Optional, NamedTuple
import numpy as np
import pyaudio


class AudioChunk(NamedTuple):
    data: np.ndarray
    pointer: int
    timestamp: float


class RecordingStream:
    def __init__(self, sample_rate, frames_per_buffer):
        self.in_stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            stream_callback=self.new_frame,
            frames_per_buffer=frames_per_buffer,
        )

        self.connections = []
        self.pointer = 0

    def connect(self, target):
        """Add new frame callback
        """
        self.connections.append(target)

    def stop(self):
        self.in_stream.stop_stream()
        self.in_stream.close()

    def new_frame(self, data, n_frames, time_info, status):
        arr = np.fromstring(data, dtype='int16')

        d = AudioChunk(arr, self.pointer, time.time())
        for target in self.connections:
            target.add_buffer(d)
        
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
            self.out_stream.write(buf.tostring())
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

    def __init__(self, in_stream:RecordingStream, out_stream:PlaybackStream, latency:float=100e-3, thread:bool=False):
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
        sample_rate = self.out_stream.sample_rate
        buf, index, play_time = data

        last_play_index, last_play_time = self.out_stream.play_head
        desired_play_time = play_time + self.latency
        desired_play_index = last_play_index + int((desired_play_time - last_play_time) * sample_rate)
        desired_index_offset = desired_play_index - last_play_index
        if abs(self.index_offset - desired_index_offset) > (20e-3 * sample_rate):
            print("adjust index offset:", self.index_offset, desired_index_offset)
            self.index_offset = desired_index_offset

        write_pointer = index + self.index_offset

        self.out_stream.add_buffer_data(buf, write_pointer)

    def stop(self):
        self.running = False
        if self.thread is not None:
            self.thread.join()

    def run(self):
        while self.running:
            data = self.data_queue.get()
            self.handle_data(data)


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    #parser.add_argument('address', type=str, help='IP address to connect to')
    args = parser.parse_args()


    pa = pyaudio.PyAudio()

    sample_rate = 22050

    mic_stream = RecordingStream(sample_rate, frames_per_buffer=128)
    out_stream = PlaybackStream(sample_rate, frames_per_buffer=128)
    mic_writer = StreamWriter(mic_stream, out_stream, latency=20e-3)

    def quit():
        out_stream.stop()
        mic_stream.stop()
        pa.terminate()

    atexit.register(quit)
