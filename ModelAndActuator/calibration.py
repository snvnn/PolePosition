"""Human-confirmed settings calibration, with immutable backing files and hashes."""
import datetime
import json
from pathlib import Path
import subprocess
from .setup import backup, digest, save_json, unique, verify_backup

CHECKS = ("windows_axes_verified", "acc_axis_assignment_verified", "direction_and_inversion_verified",
          "linear_settings_verified", "initial_neutral_verified", "released_neutral_verified",
          "xbox_disconnected", "separate_preset_saved")
SDK_HASH = "199316aaefac95118e869c07e7f332ce492dd9a8b6dac5a3ca20b1d5bb5058a1"


def read_acc(path):
    data = Path(path).read_bytes()
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-16-le" if data[:2] == b"{\x00" else "utf-8-sig")
    return json.loads(data.decode(encoding))


def draft(acc_root, backup_path, prepared, driver_path, executable="target/release/poleposition-vjoy.exe", parent="input_profiles"):
    original = verify_backup(backup_path)
    root = Path(acc_root).resolve()
    if root != Path(original["root"]).resolve():
        raise ValueError("ACC root does not match the original backup")
    prepared = Path(prepared).resolve()
    meta = json.loads((prepared / "manifest.json").read_text(encoding="utf-8"))
    if meta.get("complete") is not True or meta.get("kind") != "state_prepared":
        raise ValueError("Incomplete StateProcessor preparation")
    heading = json.loads((prepared / "heading_profile.json").read_text(encoding="utf-8"))
    probe = json.loads(subprocess.run([executable, "probe"], check=True, capture_output=True, text=True, encoding="utf-8").stdout)
    if probe["dll_sha256"] != SDK_HASH or probe["driver_api_version"] != 0x222 or probe["status"] != 1:
        raise ValueError("Wrong SDK/driver or device is occupied")
    wheel_backup = backup(root, parent)
    driver = Path(driver_path).resolve()
    if not driver.is_file():
        raise ValueError("Missing installed driver file")
    files = [root / "Config/controls.json", root / "Config/assists.json", driver,
             Path(backup_path).resolve() / "manifest.json", wheel_backup / "manifest.json"]
    output = unique(parent, "calibration-draft")
    data = {"schema_version": 1, "kind": "vjoy_calibration", "complete": False,
        "driver_release": "2.2.2.0", "dll_sha256": SDK_HASH, "probe": probe, "mapping": probe["mapping"],
        "identity": heading["identity"], "controls_path": str(files[0]), "assists_path": str(files[1]),
        "driver_path": str(driver), "backup_manifest_path": str(files[3]), "wheel_backup_path": str(wheel_backup),
        "input_sha256": {str(p): digest(p) for p in files},
        "checks": {name: {"passed": False, "evidence": ""} for name in CHECKS},
        "reviewer": "", "reviewed_at": "", "preset_name": "", "driver_file_version": "",
        "notes": "Only settings calibration. Driving response and forced-guardian-exit behavior require separate runs. Raw steering is not a virtual axis."}
    save_json(output / "calibration.json", data)
    return output / "calibration.json"


def validate_axis_bindings(device):
    axes = device.get("raceCommandAxisList", [])
    for code, index in (("Steer", 0), ("Gas", 1), ("Brake", 2)):
        matches = [axis for axis in axes if axis.get("axisCode") == code]
        if len(matches) != 1:
            raise ValueError(f"ACC active vJoy binding missing or duplicated: {code}")
        axis = matches[0]
        if (axis.get("axisIndex") != index or axis.get("isCombined") is not False
                or {axis.get("minRange"), axis.get("maxRange")} != {-1, 1}):
            raise ValueError(f"ACC active vJoy X/Y/Z binding/range mismatch: {code}")


def validate_profile(data):
    if data.get("schema_version") != 1 or data.get("kind") != "vjoy_calibration" or data.get("driver_release") != "2.2.2.0" or data.get("dll_sha256") != SDK_HASH:
        raise ValueError("Unsupported calibration profile")
    for check in CHECKS:
        record = data.get("checks", {}).get(check, {})
        if record.get("passed") is not True or not record.get("evidence", "").strip():
            raise ValueError(f"Missing verification and evidence: {check}")
    for key in ("reviewer", "reviewed_at", "preset_name", "driver_file_version"):
        if not data.get(key, "").strip():
            raise ValueError(f"Missing {key}")
    datetime.datetime.fromisoformat(data["reviewed_at"].replace("Z", "+00:00"))
    if data["driver_file_version"] != "2.2.2.0":
        raise ValueError("Driver file version must be verified as 2.2.2.0")
    for key in ("controls_path", "assists_path", "driver_path", "backup_manifest_path"):
        if data[key] not in data["input_sha256"]:
            raise ValueError("Required calibration input not hashed")
    for path, expected in data["input_sha256"].items():
        if digest(path) != expected:
            raise ValueError(f"Calibration input changed: {path}")
    verify_backup(Path(data["backup_manifest_path"]).parent)
    verify_backup(data["wheel_backup_path"])
    controls = read_acc(data["controls_path"])
    assists = read_acc(data["assists_path"])
    if controls.get("carControllerMode") != 0:
        raise ValueError("ACC wheel controller mode is required")
    device = next((d for d in controls.get("commandDevices", []) if "vjoy" in d.get("productName", "").lower()), None)
    if device is None or device.get("brakeGamma") != 1 or device.get("steerLinearity") != 1:
        raise ValueError("ACC vJoy device with brake gamma 1 and steering linearity 1 is required")
    validate_axis_bindings(device)
    # Assists use dictionaries for permitted ranges and selected values; verify the selected value.
    auto = assists.get("autoGear", assists.get("autoGearEnabled"))
    if isinstance(auto, dict):
        auto = auto.get("value", auto.get("current"))
    if auto != 1:
        raise ValueError("Automatic gears must be verified in ACC assists")
    if data["mapping"].get("device") != 1:
        raise ValueError("Device 1 is required")
    for key in ("steering", "throttle", "brake"):
        axis = data["mapping"][key]
        actual = data["probe"]["mapping"][key]
        if not (0 <= axis["min"] < axis["center"] < axis["max"] <= 65535) or not isinstance(axis["inverted"], bool):
            raise ValueError("Invalid calibrated axis")
        if axis["min"] != actual["min"] or axis["max"] != actual["max"]:
            raise ValueError("Calibrated range differs from probe")


def finalize(path, parent="input_profiles"):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_profile(data)
    result = unique(parent, "calibration")
    data["complete"] = True
    data["draft_path"] = str(path)
    data["draft_sha256"] = digest(path)
    save_json(result / "calibration.json", data)
    return result / "calibration.json"
