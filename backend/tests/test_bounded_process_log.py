import importlib.util
from pathlib import Path
import sys
import time

spec=importlib.util.spec_from_file_location('bounded_process_log',Path(__file__).resolve().parents[2]/'scripts/bounded_process_log.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def test_child_output_drained_and_rotation_bounded(tmp_path):
    p=m.start_logged([sys.executable,'-u','-c',"import sys; [print('x'*100) for _ in range(2000)]; print('LAST',file=sys.stderr)"],tmp_path/'child.log',max_bytes=2048,backups=2)
    assert p.wait(timeout=10)==0
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        if 'LAST' in (tmp_path/'child.log').read_text():break
        time.sleep(.02)
    assert 'LAST' in (tmp_path/'child.log').read_text()
    assert len(list(tmp_path.glob('child.log*')))==3
    assert max(f.stat().st_size for f in tmp_path.iterdir())<2200
