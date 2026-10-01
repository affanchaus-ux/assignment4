import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

NBD_DEVICE = "/dev/nbd0"


def run_cmd(cmd, input_text=None):
    result = subprocess.run(cmd, input=input_text, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)} -> {result.stderr.strip()}")
    return result.stdout


def qemu_info_chain(image):
    output = run_cmd(["qemu-img", "info", "--output=json", "--backing-chain", image])
    return json.loads(output)


def build_tree(chain, index=0):
    layer = chain[index]
    node = {"image": layer["filename"], "format": layer.get("format")}
    if index + 1 < len(chain):
        node["backing"] = build_tree(chain, index + 1)
    return node


def print_tree(chain):
    print("Backing chain tree (top -> base):", file=sys.stderr)
    for depth, layer in enumerate(chain):
        prefix = "" if depth == 0 else " " * (depth - 1) + "└── "
        print(f"{prefix}{layer['filename']}", file=sys.stderr)


def inspect_action(image):
    if not os.path.exists(image):
        return {"action": "inspect", "image": image, "error": "image not found"}
    chain = qemu_info_chain(image)
    top = chain[0]
    print_tree(chain)
    return {
        "action": "inspect",
        "image": image,
        "format": top.get("format"),
        "virtual_size_bytes": top.get("virtual-size"),
        "actual_size_bytes": top.get("actual-size"),
        "backing_file": top.get("full-backing-filename") or top.get("backing-filename"),
        "snapshot_count": len(top.get("snapshots", [])),
        "chain_depth": len(chain),
        "backing_chain_tree": build_tree(chain),
    }


def image_path(name):
    return name if name.endswith(".qcow2") else f"{name}.qcow2"


def write_file_into_layer(image, filename, content, make_fs=False):
    mount_dir = tempfile.mkdtemp(prefix="qcow2-mnt-")
    connected = False
    mounted = False
    try:
        run_cmd(["sudo", "qemu-nbd", "--connect", NBD_DEVICE, "--format", "qcow2", image])
        connected = True
        if make_fs:
            run_cmd(["sudo", "mkfs.ext4", "-q", NBD_DEVICE])
        run_cmd(["sudo", "mount", NBD_DEVICE, mount_dir])
        mounted = True
        run_cmd(["sudo", "tee", os.path.join(mount_dir, filename)], input_text=content)
        run_cmd(["sudo", "sync"])
    finally:
        if mounted:
            subprocess.run(["sudo", "umount", mount_dir], capture_output=True)
        if connected:
            subprocess.run(["sudo", "qemu-nbd", "--disconnect", NBD_DEVICE], capture_output=True)
        try:
            os.rmdir(mount_dir)
        except OSError:
            pass


def create_chain_action(base_name, count):
    run_cmd(["sudo", "modprobe", "nbd", "max_part=8"])
    base = image_path(base_name)
    stem = base[: -len(".qcow2")]
    images = [base] + [f"{stem}-overlay{i}.qcow2" for i in range(1, count + 1)]

    existing = [p for p in images if os.path.exists(p)]
    if existing:
        return {"action": "create-chain", "error": f"image(s) already exist: {existing}"}

    layers = []
    for i, image in enumerate(images):
        if i == 0:
            run_cmd(["qemu-img", "create", "-f", "qcow2", image, "1G"])
            backing = None
        else:
            backing = os.path.basename(images[i - 1])
            run_cmd(["qemu-img", "create", "-f", "qcow2", "-b", backing, "-F", "qcow2", image])

        filename = f"layer{i}.txt"
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        content = f"This is layer {i} ({os.path.basename(image)}) written at {timestamp}\n"
        write_file_into_layer(image, filename, content, make_fs=(i == 0))
        print(f"Created {image} and wrote {filename}", file=sys.stderr)

        layers.append({
            "layer": i,
            "image": image,
            "role": "base" if i == 0 else "overlay",
            "backing_file": backing,
            "written_file": filename,
            "content": content.strip(),
        })

    return {
        "action": "create-chain",
        "base": base,
        "overlays": count,
        "top": images[-1],
        "layers": layers,
    }


def main():
    parser = argparse.ArgumentParser(description="QCOW2 Image Analyser")
    parser.add_argument("--action", required=True,
                        choices=["inspect", "create-chain", "verify-chain", "size-report"])
    parser.add_argument("--image", help="QCOW2 image to inspect")
    parser.add_argument("--base", help="Base image name for create-chain")
    parser.add_argument("--overlays", type=int, help="Number of overlays for create-chain")
    parser.add_argument("--top", help="Top overlay for verify-chain / size-report")
    args = parser.parse_args()

    try:
        if args.action == "inspect":
            if not args.image:
                result = {"action": "inspect", "error": "--image is required"}
            else:
                result = inspect_action(args.image)
        elif args.action == "create-chain":
            if not args.base or args.overlays is None:
                result = {"action": "create-chain", "error": "--base and --overlays are required"}
            elif args.overlays < 0:
                result = {"action": "create-chain", "error": "--overlays must be 0 or more"}
            else:
                result = create_chain_action(args.base, args.overlays)
        else:
            result = {"action": args.action, "error": "not implemented yet"}
    except RuntimeError as e:
        result = {"action": args.action, "error": str(e)}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
