import json
import os
import sys
from logging.handlers import RotatingFileHandler
from unittest import mock

import pytest

import image_inspect as ii


# ---------------- helpers ----------------

def fake_qemu_info(backing_map):
    """Fake run_cmd for 'qemu-img info': returns JSON with the backing file from backing_map."""
    def fake(cmd, input_text=None):
        image = cmd[-1]
        info = {"filename": image, "format": "qcow2"}
        backing = backing_map.get(os.path.basename(image))
        if backing:
            info["backing-filename"] = backing
        return json.dumps(info)
    return fake


def make_files(folder, *names):
    for name in names:
        (folder / name).write_bytes(b"x")


@pytest.fixture
def cli(monkeypatch, tmp_path, capsys):
    """Runs main() with given CLI args inside tmp_path; returns (json_stdout, stderr_text)."""
    monkeypatch.chdir(tmp_path)

    def remove_file_handlers():
        for h in list(ii.logger.handlers):
            if isinstance(h, RotatingFileHandler):
                ii.logger.removeHandler(h)
                h.close()

    remove_file_handlers()

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["image_inspect.py", *argv])
        ii.main()
        captured = capsys.readouterr()
        return json.loads(captured.out), captured.err

    yield run
    remove_file_handlers()


# ---------------- run_cmd / qemu_info_chain ----------------

def test_run_cmd_returns_stdout_on_success():
    fake = mock.Mock(returncode=0, stdout="ok\n", stderr="")
    with mock.patch("image_inspect.subprocess.run", return_value=fake) as run:
        assert ii.run_cmd(["echo", "ok"]) == "ok\n"
    run.assert_called_once_with(["echo", "ok"], input=None, capture_output=True, text=True)


def test_run_cmd_raises_on_failure():
    fake = mock.Mock(returncode=1, stdout="", stderr="boom")
    with mock.patch("image_inspect.subprocess.run", return_value=fake):
        with pytest.raises(RuntimeError, match="boom"):
            ii.run_cmd(["false"])


def test_qemu_info_chain_uses_json_and_backing_chain():
    with mock.patch("image_inspect.run_cmd", return_value='[{"filename": "a.qcow2"}]') as rc:
        chain = ii.qemu_info_chain("a.qcow2")
    rc.assert_called_once_with(["qemu-img", "info", "--output=json", "--backing-chain", "a.qcow2"])
    assert chain == [{"filename": "a.qcow2"}]


# ---------------- Task 1: inspect ----------------

def test_image_path_adds_extension_only_when_missing():
    assert ii.image_path("demo") == "demo.qcow2"
    assert ii.image_path("demo.qcow2") == "demo.qcow2"


def test_build_tree_nests_backing_layers():
    chain = [{"filename": "top.qcow2", "format": "qcow2"},
             {"filename": "mid.qcow2", "format": "qcow2"},
             {"filename": "base.qcow2", "format": "qcow2"}]
    tree = ii.build_tree(chain)
    assert tree["image"] == "top.qcow2"
    assert tree["backing"]["image"] == "mid.qcow2"
    assert tree["backing"]["backing"]["image"] == "base.qcow2"
    assert "backing" not in tree["backing"]["backing"]


def test_inspect_missing_image_returns_error(tmp_path):
    result = ii.inspect_action(str(tmp_path / "missing.qcow2"))
    assert result["error"] == "image not found"


def test_inspect_reports_all_fields(tmp_path):
    make_files(tmp_path, "top.qcow2")
    top, base = str(tmp_path / "top.qcow2"), str(tmp_path / "base.qcow2")
    chain = [
        {"filename": top, "format": "qcow2", "virtual-size": 1073741824, "actual-size": 200704,
         "backing-filename": "base.qcow2", "full-backing-filename": base,
         "snapshots": [{"id": "1", "name": "snap1"}]},
        {"filename": base, "format": "qcow2", "virtual-size": 1073741824, "actual-size": 196608},
    ]
    with mock.patch("image_inspect.qemu_info_chain", return_value=chain):
        result = ii.inspect_action(top)
    assert result["format"] == "qcow2"
    assert result["virtual_size_bytes"] == 1073741824
    assert result["actual_size_bytes"] == 200704
    assert result["backing_file"] == base
    assert result["snapshot_count"] == 1
    assert result["chain_depth"] == 2
    assert result["backing_chain_tree"]["backing"]["image"] == base


# ---------------- Task 2: create-chain ----------------

def test_create_chain_refuses_to_overwrite_existing_images(tmp_path):
    make_files(tmp_path, "demo.qcow2")
    with mock.patch("image_inspect.run_cmd") as rc, \
         mock.patch("image_inspect.write_file_into_layer") as wf:
        result = ii.create_chain_action(str(tmp_path / "demo"), 2)
    assert "already exist" in result["error"]
    wf.assert_not_called()
    assert all(c.args[0][0] != "qemu-img" for c in rc.call_args_list)


def test_create_chain_builds_base_and_overlays(tmp_path):
    with mock.patch("image_inspect.run_cmd") as rc, \
         mock.patch("image_inspect.write_file_into_layer") as wf:
        result = ii.create_chain_action(str(tmp_path / "demo"), 2)

    assert result["top"] == str(tmp_path / "demo-overlay2.qcow2")
    assert [l["backing_file"] for l in result["layers"]] == [None, "demo.qcow2", "demo-overlay1.qcow2"]
    assert [l["written_file"] for l in result["layers"]] == ["layer0.txt", "layer1.txt", "layer2.txt"]
    assert len({l["content"] for l in result["layers"]}) == 3

    creates = [c.args[0] for c in rc.call_args_list if c.args[0][:2] == ["qemu-img", "create"]]
    assert creates[0] == ["qemu-img", "create", "-f", "qcow2", str(tmp_path / "demo.qcow2"), "1G"]
    assert creates[1] == ["qemu-img", "create", "-f", "qcow2", "-b", "demo.qcow2", "-F", "qcow2",
                          str(tmp_path / "demo-overlay1.qcow2")]
    assert [c.kwargs["make_fs"] for c in wf.call_args_list] == [True, False, False]


def test_write_file_into_layer_cleans_up_when_mount_fails():
    def fake_run_cmd(cmd, input_text=None):
        if cmd[1] == "mount":
            raise RuntimeError("mount failed")
        return ""

    with mock.patch("image_inspect.run_cmd", side_effect=fake_run_cmd), \
         mock.patch("image_inspect.subprocess.run") as sp:
        with pytest.raises(RuntimeError):
            ii.write_file_into_layer("demo.qcow2", "layer0.txt", "hi\n", make_fs=True)

    cleanup = [c.args[0] for c in sp.call_args_list]
    assert ["sudo", "qemu-nbd", "--disconnect", ii.NBD_DEVICE] in cleanup
    assert not any(cmd[1] == "umount" for cmd in cleanup)


# ---------------- Task 3: verify-chain ----------------

def test_resolve_backing_relative_and_absolute():
    assert ii.resolve_backing("/imgs/top.qcow2", "base.qcow2") == "/imgs/base.qcow2"
    assert ii.resolve_backing("/imgs/top.qcow2", "/other/base.qcow2") == "/other/base.qcow2"


def test_verify_chain_healthy(tmp_path):
    make_files(tmp_path, "top.qcow2", "mid.qcow2", "base.qcow2")
    chain = {"top.qcow2": "mid.qcow2", "mid.qcow2": "base.qcow2"}
    with mock.patch("image_inspect.run_cmd", side_effect=fake_qemu_info(chain)):
        result = ii.verify_chain_action(str(tmp_path / "top.qcow2"))
    assert result["chain_status"] == "OK"
    assert result["layers_checked"] == 3
    assert result["broken_links"] == []
    assert all(layer["status"] == "OK" for layer in result["layers"])
    assert result["layers"][-1]["backing_file"] is None


def test_verify_chain_reports_missing_layer(tmp_path):
    make_files(tmp_path, "top.qcow2")
    with mock.patch("image_inspect.run_cmd", side_effect=fake_qemu_info({"top.qcow2": "mid.qcow2"})):
        result = ii.verify_chain_action(str(tmp_path / "top.qcow2"))
    assert result["chain_status"] == "BROKEN"
    assert result["layers_checked"] == 2
    link = result["broken_links"][0]
    assert link["referenced_by"] == str(tmp_path / "top.qcow2")
    assert link["target"] == str(tmp_path / "mid.qcow2")
    assert link["reason"] == "file does not exist"


def test_verify_chain_detects_loop(tmp_path):
    make_files(tmp_path, "a.qcow2", "b.qcow2")
    loop = {"a.qcow2": "b.qcow2", "b.qcow2": "a.qcow2"}
    with mock.patch("image_inspect.run_cmd", side_effect=fake_qemu_info(loop)):
        result = ii.verify_chain_action(str(tmp_path / "a.qcow2"))
    assert result["chain_status"] == "BROKEN"
    assert result["broken_links"][0]["reason"] == "loop detected in backing chain"


def test_verify_chain_reports_invalid_image(tmp_path):
    make_files(tmp_path, "top.qcow2")
    with mock.patch("image_inspect.run_cmd", side_effect=RuntimeError("not a qcow2 image")):
        result = ii.verify_chain_action(str(tmp_path / "top.qcow2"))
    assert result["chain_status"] == "BROKEN"
    assert result["broken_links"][0]["referenced_by"] is None
    assert "qemu-img cannot open image" in result["broken_links"][0]["reason"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_verify_chain_reports_unreadable_file(tmp_path):
    top = tmp_path / "top.qcow2"
    top.write_bytes(b"x")
    top.chmod(0)
    try:
        result = ii.verify_chain_action(str(top))
    finally:
        top.chmod(0o644)
    assert result["chain_status"] == "BROKEN"
    assert result["broken_links"][0]["reason"] == "file is not readable"


# ---------------- Task 4: size-report ----------------

@pytest.mark.parametrize("value, expected", [
    (500, "500.00 B"),
    (1024, "1.00 KiB"),
    (860160, "840.00 KiB"),
    (1712128, "1.63 MiB"),
    (1073741824, "1.00 GiB"),
])
def test_human_size(value, expected):
    assert ii.human_size(value) == expected


def test_size_report_missing_top_returns_error(tmp_path):
    result = ii.size_report_action(str(tmp_path / "missing.qcow2"))
    assert result["error"] == "image not found"


def test_size_report_per_layer_and_totals(tmp_path):
    top, base = tmp_path / "top.qcow2", tmp_path / "base.qcow2"
    top.write_bytes(b"x" * 300)
    base.write_bytes(b"x" * 500)
    chain = [
        {"filename": str(top), "virtual-size": 1073741824, "actual-size": 860160},
        {"filename": str(base), "virtual-size": 1073741824, "actual-size": 1712128},
    ]
    with mock.patch("image_inspect.qemu_info_chain", return_value=chain):
        result = ii.size_report_action(str(top))

    assert [l["role"] for l in result["layers"]] == ["overlay", "base"]
    assert result["layers"][0]["usage_percent_of_virtual"] == 0.0801
    assert result["layers"][0]["file_size_bytes"] == 300
    totals = result["totals"]
    assert totals["virtual_size_seen_by_vm_bytes"] == 1073741824
    assert totals["total_actual_disk_usage_bytes"] == 860160 + 1712128
    assert totals["total_file_size_bytes"] == 800
    assert totals["footprint_percent_of_virtual"] == round((860160 + 1712128) / 1073741824 * 100, 4)


# ---------------- Task 5: JSON output + logging (main) ----------------

def test_main_missing_argument_returns_json_error(cli):
    result, _ = cli("--action", "verify-chain")
    assert result == {"action": "verify-chain", "error": "--top is required"}


def test_main_rejects_negative_overlays(cli):
    result, _ = cli("--action", "create-chain", "--base", "demo", "--overlays", "-1")
    assert result["error"] == "--overlays must be 0 or more"


def test_main_qemu_failure_becomes_json_error(cli, tmp_path):
    make_files(tmp_path, "bad.qcow2")
    with mock.patch("image_inspect.run_cmd", side_effect=RuntimeError("Command failed: qemu-img info")):
        result, _ = cli("--action", "inspect", "--image", "bad.qcow2")
    assert result["error"].startswith("Command failed")


def test_main_stdout_is_pure_json_and_tree_goes_to_stderr(cli, tmp_path):
    make_files(tmp_path, "top.qcow2", "base.qcow2")
    with mock.patch("image_inspect.run_cmd", side_effect=fake_qemu_info({"top.qcow2": "base.qcow2"})):
        result, err = cli("--action", "verify-chain", "--top", "top.qcow2")
    assert result["chain_status"] == "OK"
    assert "Verifying chain" in err


def test_main_writes_rotating_log_file(cli, tmp_path):
    cli("--action", "inspect", "--image", "missing.qcow2")
    log_text = (tmp_path / "image_inspect.log").read_text()
    assert "INFO action=inspect started" in log_text
    assert "ERROR action=inspect error: image not found" in log_text
