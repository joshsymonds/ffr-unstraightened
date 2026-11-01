"""provenance: fail when repo history names withheld private terms.

Scans every commit reachable from HEAD (tree paths, blob text, commit messages)
for tokens whose sha256 digest is in STORED_DIGESTS. Output carries locations and
a 12-character digest prefix, never the matched text. Run as `python3 -m ffrd.provenance`.
"""

import hashlib
import re
import subprocess
import sys

STORED_DIGESTS: frozenset[str] = frozenset(
    {
        "00b447f672d78b4037b17bf4c9cd3261e2ac9de3e505dd5ff8b96bc6edca91bb",
        "10c2082282acbbe6a5050a482ac1d8213224ffcf1ef6deff40f68637fc019cd0",
        "141f7784dcf43813913a3bb558ae88262e445a8aa687bbc04d09d4f9d68108bc",
        "192b8334c0f325be5d4b727338bbd3f589d1a66789057c565b77a737c6aad618",
        "2c4f900de8ef8feabcd64ff8ec36ce937f1a9987cc28c192ee66183039e5c78b",
        "2e59e530d5c8680d17f79db041c838f456e885f75db4d3f7c6d5dd67fb662d53",
        "34351129e34b8c1ce981f3ad20d78c14f4bd9fb603f39a20324e78f2e676ac22",
        "3d509501381bc83dddeca188beb42f848b66037346e5aeaa6ad5f1d1bb1cce57",
        "40ed2a5f438cd514bbf54b4610d783c662095a67fe97a59bf36e01518421f7da",
        "4efcdc489a4ad6cb1ee7fb01308bb8f85f21ee23b9e9f298e3e33a70a4fc0e8c",
        "5034db86fa9f2d3860fa3b8875496dfec1d1fc1a6a059541c93ab71f11563475",
        "5567732bb5446568697382f211dc989e3e0f3de4ff592fe465e763f96f7801ce",
        "67683cba51f564e21eeafc3d0a9803e51ff22baaa49c873bf9a60010004b4816",
        "6c3f00d533122362e9af99838d8c7808daea4875b6bc00f0e4336c7b30a61c15",
        "7047432b39b983526e3f07743e9f990325348d2313dd0bcb3c7f92debfd81f62",
        "73b5e1f0f90aff5bc00445ddda9253825656786e90f8b824722bf53af4f36082",
        "797969c486b97d63d961d94f594d72b333096a1870943df2bb432721f2cb4b85",
        "7c629cb60cea898ed5aa549c937eeab2994fd2bf9f4d4c354251390c48182800",
        "8420d30b644564f4d7bff4a9d5ce2fa7c10cd07b1e0149b75c5b55fce1181b25",
        "85749888e199cf1f37f73c1c37a23a976f3a7ce7b4d75d071941c80d935215ee",
        "889bc9c21f29dadf1ec66def76715e84329ec7d6387429b7b410e17c76770662",
        "89fbc17638769666dc5631f87b85c63fbef5c1c94641740b5e1d3546e504bbb6",
        "8e3011e84d7270241c9b9fe8e30de24dd06c1b270df438c684611005f38aed8c",
        "9242ce2163e78f6e16dfddaf5bf7a1fa2ecfbbbe416e0b6d116700354ed88214",
        "9ada765b18a1066b499256d256da09e334ea69c8671eac4f743ae29ec00283c5",
        "9d28e4e6f13f210890073a4f4d8ca363805c6086f6c38eff785498acc71ad267",
        "b02cca0384b97ce283f94506cf27928c7163787b70058b005439ad01055d50ed",
        "b7a66b6583b1215c79197c069bea15dbbe45b444f3ac6684c50b547b14676c11",
        "c12c212d0d29a02a12732b17a52d64bcf80e68b511d89d5ce5c77abb9188ad73",
        "c153bac7d164a9b89496d7d55d897fcf394160a843503ddcb1a42f173367fe17",
        "de5edb67a1500b1728c2bf9b5e0b015f79ec4e2be96ddd9bfd0147d941fdd20b",
        "e46612bbea431a7e6923a598cc97fc94bc60c0ce693d650df55a64944a51128c",
        "e52647b0ef5decef99317291daf1f9cc493743d131e06a440de27951156f0a86",
        "e8c6874f19feaa0b3c2f53bfecc4475b464dfc3e67cdeff1c33b9e55d459e633",
        "ed990ff6103f3b53e64285ea4a6e8b4dabdf4824671620aae49902c04d890d98",
        "eec20e6c8438808a78db437ff6bf5209ec0bdd2908b2d78a6f4638f52da8a23f",
        "f14bad02cccdfcbb499a8561994c598d27e8b1ef50b4962fa8a52285a1b76fd5",
        "f37538a63ae254c82d7778980ca0d2e4709e6a6d8a0b4e257058596ab7150dc7",
    }
)

RUN = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
SEPARATOR = re.compile(r"[-_]")
REDACTED = "<redacted>"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hits(text: str, digests: frozenset[str]) -> set[str]:
    """Return the 12-character prefixes of every digest matched by a token in text."""
    found: set[str] = set()
    for run in RUN.finditer(text.lower()):
        parts = SEPARATOR.split(run.group())
        for start in range(len(parts)):
            for end in range(start + 1, len(parts) + 1):
                window = parts[start:end]
                for token in {"-".join(window), "".join(window)}:
                    current = digest(token)
                    if current in digests:
                        found.add(current[:12])
    return found


def redact(path: str, digests: frozenset[str]) -> str:
    """Hide every path component that contains a match, so output never shows the term."""
    return "/".join(REDACTED if hits(part, digests) else part for part in path.split("/"))


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout


def scan(digests: frozenset[str]) -> list[str]:
    """Return one sorted report line per hit across every commit reachable from HEAD."""
    report: set[str] = set()
    blob_hits: dict[str, set[str]] = {}
    for commit in git("rev-list", "HEAD").decode().split():
        for entry in git("ls-tree", "-r", "-z", commit).split(b"\0"):
            if not entry:
                continue
            meta, _, raw_path = entry.partition(b"\t")
            _, kind, oid = meta.decode().split()
            path = raw_path.decode("utf-8", errors="replace")
            location = f"{commit}:{redact(path, digests)}"
            for prefix in hits(path, digests):
                report.add(f"{location} {prefix}")
            if kind != "blob":
                continue
            if oid not in blob_hits:
                text = git("cat-file", "blob", oid).decode("utf-8", errors="replace")
                blob_hits[oid] = hits(text, digests)
            for prefix in blob_hits[oid]:
                report.add(f"{location} {prefix}")
        message = git("show", "-s", "--format=%B", commit).decode("utf-8", errors="replace")
        for prefix in hits(message, digests):
            report.add(f"{commit}:message {prefix}")
    return sorted(report)


def main() -> int:
    try:
        report = scan(STORED_DIGESTS)
    except subprocess.CalledProcessError as err:
        print(f"provenance: git failed: {err.stderr.decode(errors='replace').strip()}", file=sys.stderr)
        return 2
    for line in report:
        print(line)
    return 1 if report else 0


if __name__ == "__main__":
    sys.exit(main())
