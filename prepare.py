"""Prepare an owned runtime from a verified, isolated upstream Git checkout.

Example:
    python prepare.py --source <miao-checkout> --dest <isolated-runtime>

This never executes Yunzai, migrates old player files, or installs dependencies.
Install the runtime's locked npm dependencies separately after preparation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

SOURCE_COMMIT = "850d854f24dab92a7d707cc448dbe34d19580dd6"
SOURCE_REPO = "https://github.com/yoimiya-kokomi/miao-plugin"
HERE = Path(__file__).resolve().parent


def prepare(source: Path, destination: Path) -> dict:
    """Copy tracked files and apply exact, recorded compatibility adaptations.

    Args:
        source: A clean checkout at the locked Miao commit.
        destination: A new directory or an owned runtime of the same commit.

    Returns:
        A preparation receipt with the source commit and available file counts.

    Raises:
        ValueError: If ownership, source state, or an exact patch check fails.
    """
    source, destination = source.resolve(), destination.resolve()
    if (
        source == destination
        or source in destination.parents
        or destination in source.parents
    ):
        raise ValueError("Source and runtime must be separate directories")
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != SOURCE_COMMIT:
        raise ValueError("Unexpected upstream commit")
    status = subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"],
        text=True,
    )
    if status.strip():
        raise ValueError("Upstream checkout has tracked changes")
    marker = destination / "runtime-receipt.json"
    if destination.exists() and any(destination.iterdir()):
        if not marker.is_file():
            raise ValueError("Refusing to overwrite an unowned runtime directory")
        saved = json.loads(marker.read_text(encoding="utf-8"))
        if (
            saved.get("owner") != "astrbot_plugin_miao"
            or saved.get("source_commit") != SOURCE_COMMIT
        ):
            raise ValueError("A different runtime needs a separate staging directory")
        receipt = saved
        if not saved.get("assets_complete"):
            paths = (
                subprocess.check_output(
                    [
                        "git",
                        "-C",
                        str(source),
                        "ls-tree",
                        "-r",
                        "--name-only",
                        "-z",
                        "HEAD",
                    ]
                )
                .decode()
                .rstrip("\0")
                .split("\0")
            )
            missing = 0
            for relative in paths:
                origin = source / relative
                target = destination / "plugins" / "miao-plugin" / relative
                if target.exists():
                    continue
                if not origin.is_file():
                    missing += 1
                    continue
                if origin.is_symlink() or not origin.resolve().is_relative_to(source):
                    raise ValueError("Upstream file escapes its checkout")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, target)
                saved["original_sha256"][relative] = hashlib.sha256(
                    target.read_bytes()
                ).hexdigest()
            receipt["copied_files"] = len(saved["original_sha256"])
            receipt["missing_files"] = missing
            receipt["assets_complete"] = missing == 0
            marker.write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    else:
        destination.mkdir(parents=True, exist_ok=True)
        paths = (
            subprocess.check_output(
                ["git", "-C", str(source), "ls-tree", "-r", "--name-only", "-z", "HEAD"]
            )
            .decode()
            .rstrip("\0")
            .split("\0")
        )
        tracked = {}
        missing = []
        vendor = destination / "plugins" / "miao-plugin"
        for relative in paths:
            origin = source / relative
            if not origin.is_file():
                missing.append(relative)
                continue
            if origin.is_symlink() or not origin.resolve().is_relative_to(source):
                raise ValueError("Upstream file escapes its checkout")
            target = vendor / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)
            tracked[relative] = hashlib.sha256(target.read_bytes()).hexdigest()

        version = vendor / "components" / "Version.js"
        content = version.read_text(encoding="utf-8")
        old = "const yunzaiVersion = packageJson.version"
        if content.count(old) != 1 or content.count("let Version = {") != 1:
            raise ValueError("Version compatibility patch no longer applies")
        content = content.replace(
            old,
            "const yunzaiVersion = packageJson.astrbotVersion || packageJson.version",
        )
        content = content.replace(
            "let Version = {",
            "// The adapter implements the Miao runtime contract without running Yunzai.\nif (packageJson.name === 'astrbot-miao-runtime') { name = 'AstrBot'; isMiao = true }\n\nlet Version = {",
        )
        version.write_text(content, encoding="utf-8")
        receipt = {
            "owner": "astrbot_plugin_miao",
            "source_repo": SOURCE_REPO,
            "source_commit": SOURCE_COMMIT,
            "copied_files": len(tracked),
            "missing_files": len(missing),
            "assets_complete": not missing,
            "original_sha256": tracked,
            "adaptations": ["components/Version.js: AstrBot runtime identity"],
        }
        marker.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # Every entry is (upstream text, replacement, note). The note lands in the
    # receipt, so an operator can tell a runtime shim from a data correction.
    adaptations = {
        "models/Base.js": (
            "  _getCache (uuid = '', time = 10 * 60) {\n",
            "  _getCache (uuid = '', time = 10 * 60) {\n    if (globalThis.miaoCacheKey) uuid = globalThis.miaoCacheKey(uuid)\n",
            "isolated AstrBot runtime compatibility",
        ),
        "components/Data.js": (
            "  return root\n}",
            "  if (globalThis.miaoDataRoot && root === `${_path}/`) return globalThis.miaoDataRoot()\n  return root\n}",
            "isolated AstrBot runtime compatibility",
        ),
        "models/serv/api/EnkaApi.js": (
            "HttpsProxyAgent ? HttpsProxyAgent.default : undefined",
            "HttpsProxyAgent ? (HttpsProxyAgent.HttpsProxyAgent || HttpsProxyAgent.default) : undefined",
            "isolated AstrBot runtime compatibility",
        ),
        # The default 官服 profile source is MiniGG. Measured 2026-11-01: that host
        # answers without avatarInfoList while enka.network answers in full, so a
        # fresh install reports "展柜未开启" for a player whose showcase is public.
        "models/serv/Serv.js": (
            "let defServKey = isGs ? ['mgg', 'mgg', 'enka']",
            "let defServKey = isGs ? ['enka', 'enka', 'enka']",
            "default GS showcase source is enka, measured 2026-11-01",
        ),
        "models/serv/ProfileReq.js": (
            "          const character = await mys.getCharacter()\n          const character_ids = lodash.map(character.list, (c) => c.id)",
            "          if (!mys) { self._isReq = false; return false }\n          const character = await mys.getCharacter()\n          if (!character || !Array.isArray(character.list) || character.list.length === 0) {\n            self._isReq = false\n            if (!this.e._isReplyed) { await this.e.reply('米游社未返回角色列表，本次保留已有缓存'); this.e._isReplyed = true }\n            return false\n          }\n          const character_ids = lodash.map(character.list, (c) => c.id)",
            "keep the cached panel when 米游社 returns no character list",
        ),
        # Upstream 850d854 wrote the 7.2 pool end date as 22026-11-24 (one digit
        # too many). The gacha card prints from/to verbatim, so the typo would
        # reach users as a nonsense year; the gacha stats also bucket by that date.
        # Both strings carry the `to: '` prefix on purpose — see the guard below.
        "resources/meta-gs/info/pool.js": (
            "to: '22026-11-24 17:59:59'",
            "to: '2026-11-24 17:59:59'",
            "upstream 7.2 pool end-date typo (22026) corrected to 2026",
        ),
    }
    for relative, (before, after, note) in adaptations.items():
        # "already applied" is decided by looking for `after` in the file. That only
        # works while `after` cannot be found in the unpatched text: measured
        # 2026-11-05, a bare "2026-11-24" sits inside "22026-11-24", so the typo
        # shipped while the receipt claimed it was corrected. Refuse such a pair.
        if after in before:
            raise ValueError(
                f"Adaptation for {relative} cannot be verified: replacement is inside the original text"
            )
        target = destination / "plugins" / "miao-plugin" / relative
        content = target.read_text(encoding="utf-8")
        if after not in content:
            if content.count(before) != 1:
                raise ValueError(
                    f"Compatibility adaptation no longer applies: {relative}"
                )
            target.write_text(content.replace(before, after), encoding="utf-8")
        label = f"{relative}: {note}"
        if label not in receipt["adaptations"]:
            receipt["adaptations"].append(label)
    for relative in ("apps/profile/ProfileCommon.js", "models/serv/ProfileReq.js"):
        target = destination / "plugins" / "miao-plugin" / relative
        content = target.read_text(encoding="utf-8")
        target.write_text(
            content.replace("米游社更新面板", "更新面板"), encoding="utf-8"
        )
        label = f"{relative}: unified panel update command hints"
        if label not in receipt["adaptations"]:
            receipt["adaptations"].append(label)
    marker.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Adapter updates are safe to repeat; player files and custom config are not copied again.
    bridge = destination / "bridge"
    bridge.mkdir(exist_ok=True)
    for file in (HERE / "worker").glob("*.mjs"):
        shutil.copyfile(file, bridge / file.name)
    templates = destination / "plugins" / "miao-plugin" / "resources" / "astrbot"
    templates.mkdir(parents=True, exist_ok=True)
    for file in (HERE / "worker" / "templates").glob("*.html"):
        shutil.copyfile(file, templates / file.name)
    shutil.copyfile(HERE / "worker" / "package.json", destination / "package.json")
    lock = HERE / "worker" / "package-lock.json"
    if lock.is_file():
        shutil.copyfile(lock, destination / lock.name)
    common = destination / "lib" / "common"
    common.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "worker" / "common.mjs", common / "common.js")
    for relative in (
        "data/UserData",
        "data/PlayerData/gs",
        "data/PlayerData/sr",
        "data/gachaJson",
        "data/srJson",
        "temp/html",
        "output",
    ):
        (destination / relative).mkdir(parents=True, exist_ok=True)
    return {key: value for key, value in receipt.items() if key != "original_sha256"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.dest), ensure_ascii=False, indent=2))
