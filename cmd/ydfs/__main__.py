import argparse
import json
import os
import sys
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from fuse import FUSE

from adapter.fusefs import FuseFilesystem
from adapter.smoke import FixedFile
from core.api.client import Client, MIB
from core.fs import Filesystem


def main():
    parser = argparse.ArgumentParser(description="Mount a YDFS test filesystem.")
    parser.add_argument("--mount", required=True)
    parser.add_argument("--config", type=Path, help="JSON configuration (default with no arguments: ydfs.local.json)")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--base-url")
    parser.add_argument("--root-file-id")
    parser.add_argument("--user-domain-id", help="Enable live China Mobile Cloud capacity reporting")
    parser.add_argument("--client-info", default="ydfs-demo")
    parser.add_argument("--app-channel", default="ydfs-demo")
    parser.add_argument("--hash-algo", choices=("sha1", "sha256"), default="sha256")
    parser.add_argument("--meta-ttl", type=float, default=5)
    parser.add_argument("--block-size", type=int, default=4 * MIB)
    parser.add_argument("--part-size", type=int, default=10 * MIB)
    parser.add_argument("--cache-dir", type=Path)
    argv = sys.argv[1:] or ["--config", "ydfs.local.json"]
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", type=Path)
    config_args, _ = config_parser.parse_known_args(argv)
    auth = os.environ.get("YDFS_AUTH")
    if config_args.config:
        try:
            config = json.loads(config_args.config.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            parser.error("unable to read configuration as JSON")
        if not isinstance(config, dict):
            parser.error("configuration must be a JSON object")
        auth = config.pop("authorization", auth)
        if auth is not None and not isinstance(auth, str):
            parser.error("authorization must be a string")
        config_options = []
        value_keys = {"mount", "base_url", "root_file_id", "client_info", "app_channel",
                      "hash_algo", "meta_ttl", "block_size", "part_size", "cache_dir", "user_domain_id"}
        for key, value in config.items():
            option = "--" + key.replace("_", "-")
            if key in {"smoke", "read_only"} and isinstance(value, bool):
                if value:
                    config_options.append(option)
            elif key in value_keys and type(value) in {str, int, float}:
                config_options.append(f"{option}={value}")
            else:
                parser.error(f"invalid configuration field: {key}")
        argv = config_options + argv
    args = parser.parse_args(argv)
    windows = os.name == "nt"
    options = {"uid": -1, "gid": -1, "volname": "YDFS"} if windows else {}
    if windows:
        # Windows checks the reported filesystem name when elevating executables.
        options["ExactFileSystemName"] = "NTFS"
    if args.smoke:
        FUSE(FixedFile(), args.mount, foreground=True, ro=True, **options)
        return
    if not args.base_url or not auth:
        parser.error("base URL and authorization are required (JSON authorization or YDFS_AUTH)")
    url = urlsplit(args.base_url)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.query or url.fragment:
        parser.error("invalid base URL")
    loopback = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if not loopback and (url.scheme != "https" or not args.root_file_id or args.root_file_id == "/"):
        parser.error("remote access requires HTTPS and a dedicated test directory file ID")
    if args.block_size <= 0 or args.meta_ttl < 0:
        parser.error("invalid cache options")
    default_cache = Path(os.environ["LOCALAPPDATA"]) if windows else Path.home() / ".cache"
    cache_dir = args.cache_dir or default_cache / "ydfs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    client = Client(args.base_url, auth, args.client_info, args.app_channel,
                    args.hash_algo, args.part_size)
    disk_space = None
    if args.user_domain_id:
        disk_client = Client("https://user-njs.yun.139.com/user/disk", auth,
                             args.client_info, args.app_channel)
        disk_space = partial(disk_client.personal_disk_space, args.user_domain_id)
    fs = Filesystem(client, cache_dir, windows, args.meta_ttl, args.block_size,
                    args.root_file_id or "/", disk_space=disk_space)
    options.update(direct_io=True)
    if args.read_only:
        options["ro"] = True
    FUSE(FuseFilesystem(fs, args.read_only), args.mount, foreground=True, **options)


if __name__ == "__main__":
    main()
