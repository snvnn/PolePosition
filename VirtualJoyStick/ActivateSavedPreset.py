"""Apply the already user-saved PolePosition-vJoy preset while ACC is closed."""
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ModelAndActuator.setup import backup, digest, save_json
from ModelAndActuator.calibration import read_acc, validate_axis_bindings


def main():
    processes = subprocess.run(
        [str(Path(os.environ["WINDIR"]) / "System32/tasklist.exe"),
         "/FI", "IMAGENAME eq AC2-Win64-Shipping.exe", "/FO", "CSV", "/NH"],
        check=True, capture_output=True,
    ).stdout.lower()
    if b"ac2-win64-shipping.exe" in processes:
        raise ValueError("Close ACC before applying the saved preset")
    acc = Path.home() / "Documents/Assetto Corsa Competizione"
    source = acc / "Customs/Controls/PolePosition-vJoy.json"
    target = acc / "Config/controls.json"
    controls = read_acc(source)
    if controls.get("carControllerMode") != 0:
        raise ValueError("Saved preset is not wheel mode")
    device = next(d for d in controls["commandDevices"] if d.get("productName") == "vJoy Device")
    validate_axis_bindings(device)
    if device.get("steerLinearity") != 1 or device.get("brakeGamma") != 1:
        raise ValueError("Saved preset must have linear steering and brake gamma 1")
    preserved = backup(acc, ROOT / "input_profiles")
    data = source.read_bytes()
    before_hash = digest(target)
    temporary = target.with_name(target.name + ".poleposition-" + uuid.uuid4().hex + ".tmp")
    with temporary.open("xb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    if digest(target) != before_hash or source.read_bytes() != data:
        temporary.unlink()
        raise ValueError("ACC settings changed during preset application")
    os.replace(temporary, target)
    if target.read_bytes() != data:
        raise ValueError("Active preset verification failed")
    save_json(preserved / "preset-activation.json", {
        "complete": True, "source": str(source), "target": str(target),
        "before_sha256": before_hash, "after_sha256": digest(target),
        "source_sha256": digest(source), "backup": str(preserved),
        "scope": "Active controls file only; actual next-start ACC response still requires observation",
    })
    print(preserved / "preset-activation.json")


if __name__ == "__main__":
    main()
