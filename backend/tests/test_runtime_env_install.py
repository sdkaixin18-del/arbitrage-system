from pathlib import Path
import stat
import subprocess


HELPER = Path(__file__).resolve().parents[2] / "scripts/install_runtime_env.sh"


def run(source, target, mode="preserve"):
    return subprocess.run(["bash", str(HELPER), str(source), str(target), mode], capture_output=True, text=True)


def test_existing_runtime_config_is_preserved_even_if_source_missing(tmp_path):
    target = tmp_path / "runtime.env"
    target.write_text("LIVE=true\n")
    assert run(tmp_path / "absent", target).returncode == 0
    assert target.read_text() == "LIVE=true\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.backup.*"))


def test_first_install_and_explicit_replacement_keep_private_backup(tmp_path):
    source, target = tmp_path / "source.env", tmp_path / "nested/runtime.env"
    source.write_text("VALUE=old\n")
    assert run(source, target).returncode == 0
    source.write_text("VALUE=new\n")
    assert run(source, target, "replace").returncode == 0
    assert target.read_text() == source.read_text()
    backup, = target.parent.glob("*.backup.*")
    assert backup.read_text() == "VALUE=old\n"
    assert stat.S_IMODE(backup.stat().st_mode) == stat.S_IMODE(target.stat().st_mode) == 0o600


def test_replacement_missing_source_leaves_live_config_untouched(tmp_path):
    target = tmp_path / "runtime.env"
    target.write_text("LIVE=true\n")
    assert run(tmp_path / "absent", target, "replace").returncode != 0
    assert target.read_text() == "LIVE=true\n"


def test_symlink_target_is_rejected(tmp_path):
    source, target = tmp_path / "source.env", tmp_path / "runtime.env"
    source.write_text("LIVE=true\n")
    target.symlink_to(source)
    assert run(source, target, "replace").returncode != 0


def test_deployer_defaults_to_preservation_and_checks_effective_config():
    deploy = HELPER.with_name("deploy_local_runtime.sh")
    text = deploy.read_text()
    assert 'CONFIG_MODE=preserve' in text
    assert '--update-runtime-config) CONFIG_MODE=replace' in text
    assert 'python3 - "$LAUNCHER_ROOT/runtime.env"' in text
    assert subprocess.run(["bash", "-n", str(deploy)], capture_output=True).returncode == 0
