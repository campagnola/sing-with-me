Sing With Me
============

Low-latency network audio streaming.


Motivation
==========

Quarantine sucks, and typical latencies in internet communication apps makes it almost impossible to sing with my friends. This project is an attempt to achieve low latency (~20-50 ms) from microphone in one house to headphone in another.


How to use
==========

This is a prototype project! I've only tested it on a few machines; good luck.

You'll need:

- A python environment with numpy and pyaudio.
- UDP port 31415 accessible between both machines you want to connect. If you're using a typical home router, that means forwarding UDP port 31415 from the router to your machine. (Beware: this software has not been designed or tested for security.)

On one machine, run

```
python -i sing_with_me.py
```

On the other, run

```
python -i sing_with_me.py <ip_address>:31415
```

replacing `<ip_address>` with the address of the first machine. If all has gone well, you should now have shared audio between the two machines.

If you hear crackling, you may need to increase the target latency or other buffering parameters (currently hardcoded).


Implementation
==============

* We use UDP to stream audio packets between machines. This means that some packets may be lost in transit, but for that price we get much lower latency.
* Input from your microphone is also echoed locally with the same latency as the remote audio, so that both sides are properly synchronized in your headphones. 
* The initial handshake attempts to measure the offset between the two system clocks so we can measure actual audio latency.

