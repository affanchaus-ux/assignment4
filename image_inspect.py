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


def resolve_backing(image, backing):
    if os.path.isabs(backing):
        return backing
    return os.path.join(os.path.dirname(image), backing)


def verify_chain_action(top):
    layers = []
    broken_links = []
    visited = set()
    current = top
    parent = None
    depth = 0

    print("Verifying chain (top -> base):", file=sys.stderr)
    while current:
        entry = {"depth": depth, "image": current}
        real = os.path.realpath(current)
        reason = None

        if real in visited:
            reason = "loop detected in backing chain"
        elif not os.path.exists(current):
            reason = "file does not exist"
        elif not os.access(current, os.R_OK):
            reason = "file is not readable"

        info = None
        if reason is None:
            visited.add(real)
            try:
                info = json.loads(run_cmd(["qemu-img", "info", "--output=json", current]))
            except RuntimeError as e:
                reason = f"qemu-img cannot open image: {e}"

        indent = "" if depth == 0 else " " * (depth - 1) + "└── "
        if reason:
            entry.update({"exists": os.path.exists(current), "status": "BROKEN", "reason": reason})
            broken_links.append({"referenced_by": parent, "target": current, "reason": reason})
            layers.append(entry)
            print(f"{indent}{current} [BROKEN: {reason}]", file=sys.stderr)
            break

        backing = info.get("backing-filename")
        next_image = resolve_backing(current, backing) if backing else None
        entry.update({
            "exists": True,
            "readable": True,
            "format": info.get("format"),
            "backing_file": next_image,
            "status": "OK",
        })
        layers.append(entry)
        print(f"{indent}{current} [OK]", file=sys.stderr)

        parent = current
        current = next_image
        depth += 1

    return {
        "action": "verify-chain",
        "top": top,
        "chain_status": "BROKEN" if broken_links else "OK",
        "layers_checked": len(layers),
        "layers": layers,
        "broken_links": broken_links,
    }


def human_size(num_bytes):
    size = float(num_bytes)
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if size < 1024 or unit == "TiB":
            return f"{size:.2f} {unit}"
        size /= 1024


def size_report_action(top):
    if not os.path.exists(top):
        return {"action": "size-report", "top": top, "error": "image not found"}
    chain = qemu_info_chain(top)

    layers = []
    total_actual = 0
    total_file = 0
    print(f"{'LAYER':<28}{'VIRTUAL':>14}{'ACTUAL':>14}{'USAGE %':>10}", file=sys.stderr)
    for depth, layer in enumerate(chain):
        image = layer["filename"]
        virtual = layer.get("virtual-size", 0)
        actual = layer.get("actual-size", 0)
        file_size = os.path.getsize(image)
        usage = round(actual / virtual * 100, 4) if virtual else 0.0
        total_actual += actual
        total_file += file_size

        layers.append({
            "depth": depth,
            "image": image,
            "role": "base" if depth == len(chain) - 1 else "overlay",
            "virtual_size_bytes": virtual,
            "virtual_size_human": human_size(virtual),
            "actual_size_bytes": actual,
            "actual_size_human": human_size(actual),
            "file_size_bytes": file_size,
            "usage_percent_of_virtual": usage,
        })
        print(f"{image:<28}{human_size(virtual):>14}{human_size(actual):>14}{usage:>9.4f}%",
              file=sys.stderr)

    top_virtual = chain[0].get("virtual-size", 0)
    print(f"{'TOTAL CHAIN FOOTPRINT':<28}{human_size(top_virtual):>14}{human_size(total_actual):>14}",
          file=sys.stderr)

    return {
        "action": "size-report",
        "top": top,
        "chain_depth": len(chain),
        "layers": layers,
        "totals": {
            "virtual_size_seen_by_vm_bytes": top_virtual,
            "virtual_size_seen_by_vm_human": human_size(top_virtual),
            "total_actual_disk_usage_bytes": total_actual,
            "total_actual_disk_usage_human": human_size(total_actual),
            "total_file_size_bytes": total_file,
            "footprint_percent_of_virtual": round(total_actual / top_virtual * 100, 4) if top_virtual else 0.0,
        },
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
        elif args.action == "verify-chain":
            if not args.top:
                result = {"action": "verify-chain", "error": "--top is required"}
            else:
                result = verify_chain_action(args.top)
        elif args.action == "size-report":
            if not args.top:
                result = {"action": "size-report", "error": "--top is required"}
            else:
                result = size_report_action(args.top)
    except RuntimeError as e:
        result = {"action": args.action, "error": str(e)}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
