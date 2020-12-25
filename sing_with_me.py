import sys, threading, atexit, queue, socket, time, argparse
import numpy as np
import pyaudio

sample_rate = 22050
frames_per_buffer = 256
bytes_per_buffer = frames_per_buffer * 2
buffer_duration = frames_per_buffer / sample_rate

latency = 40e-3
latency_buffers = int(latency / buffer_duration)

empty_frame = b'\0' * bytes_per_buffer

ring_buffer = np.zeros(frames_per_buffer * int(10.0 / buffer_duration), dtype='int16')
read_pointer = 0


parser = argparse.ArgumentParser()
#parser.add_argument('address', type=str, help='IP address to connect to')
args = parser.parse_args()



pa = pyaudio.PyAudio()

write_pointer = 0

def in_callback(data, n_frames, time_info, status):
    global write_pointer, read_pointer, ring_buffer

    desired_write_pointer = int(read_pointer + latency * sample_rate)
    if abs(write_pointer - desired_write_pointer) > (50e-3 * sample_rate):
        print("adjust write pointer:", write_pointer, desired_write_pointer)
        write_pointer = desired_write_pointer
    ring_ptr = write_pointer % len(ring_buffer)
    arr = np.fromstring(data, dtype=ring_buffer.dtype)

    # write data into ring buffer, possibly in two parts if it wraps around to the beginning
    first_size = min(len(arr), len(ring_buffer) - ring_ptr)
    ring_buffer[ring_ptr:ring_ptr+first_size] += arr[:first_size]
    if first_size < len(arr):
        second_size = len(arr) - first_size
        ring_buffer[:second_size] = arr[first_size:]
    
    write_pointer += len(arr)

    return None, pyaudio.paContinue

# def out_callback(data, n_frames, time_info, status):
#     if in_queue.qsize() == 0:
#         return empty_frame, pyaudio.paContinue
#     while in_queue.qsize() > 2:
#         in_queue.get()
#     return in_queue.get()[1], pyaudio.paContinue

in_stream = pa.open(
    format=pyaudio.paInt16,
    channels=1,
    rate=sample_rate,
    input=True,
    stream_callback=in_callback,
    frames_per_buffer=frames_per_buffer,
)

out_stream = pa.open(
    format=pyaudio.paInt16,
    channels=1,
    rate=sample_rate,
    output=True,
)

running = True

# def record():
#     last_time = time.time()
#     while running:
#         d = in_stream.read(8192)
#         in_queue.put(d)
#         now = time.time()
#         print(len(d) / (now - last_time))
#         last_time = now

# in_thread = threading.Thread(target=record, daemon=True)
# in_thread.start()


def play():
    global read_pointer, ring_buffer

    start_time = time.time()

    # pre-load so there is always at least 1 buffer of extra data queued
    out_stream.write(empty_frame)

    while running:
        ring_ptr = read_pointer % len(ring_buffer)

        # sleep until time to play the next chunk
        now = time.time()
        next_play_time = start_time + read_pointer / sample_rate
        dt = next_play_time - now
        if dt > 0:
            time.sleep(dt)
        
        buf = ring_buffer[ring_ptr:ring_ptr+frames_per_buffer]
        out_stream.write(buf.tostring())
        ring_buffer[ring_ptr:ring_ptr+frames_per_buffer] = 0
        read_pointer += frames_per_buffer


out_thread = threading.Thread(target=play, daemon=True)
out_thread.start()




def quit():
    running = False
    in_stream.stop_stream()
    in_stream.close()
    out_stream.stop_stream()
    out_stream.close()
    pa.terminate()

atexit.register(quit)
