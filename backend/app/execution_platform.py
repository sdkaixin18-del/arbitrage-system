"""OS adapters only; trading rules stay in their existing modules."""
import os, queue, threading
if os.name != 'nt':
 import fcntl as file_lock
else:
 import msvcrt
 class file_lock:
  LOCK_EX=2;LOCK_NB=4;LOCK_UN=8
  @staticmethod
  def flock(handle,operation):
   fd=handle if isinstance(handle,int) else handle.fileno()
   os.lseek(fd,0,os.SEEK_SET)
   try:msvcrt.locking(fd,msvcrt.LK_UNLCK if operation==8 else msvcrt.LK_NBLCK,1)
   except OSError as exc:raise BlockingIOError(str(exc)) from exc

class PipeLines:
 """Read subprocess pipes without Windows-incompatible select(pipe)."""
 def __init__(self):self.lines=queue.Queue();self.current=None;self.closed=False
 def register(self,stream,*args):
  def read():
   try:
    while not self.closed:
     line=stream.readline();self.lines.put(line)
     if not line:break
   except (OSError,ValueError):self.lines.put('')
  threading.Thread(target=read,daemon=True).start()
 def select(self,timeout=.5):
  try:self.current=self.lines.get(timeout=timeout);return [True]
  except queue.Empty:return []
 def readline(self):return self.current
 def close(self):self.closed=True
