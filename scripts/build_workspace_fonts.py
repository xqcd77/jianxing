"""Build local OFL webfont subsets; fonttools and brotli are build tools only."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".data/ui-font-tools"))


def main():
    from fontTools import subset
    from fontTools.ttLib import TTFont
    import httpx

    parser = argparse.ArgumentParser()
    parser.add_argument("--sans", type=Path, default=Path("C:/Windows/Fonts/NotoSansSC-VF.ttf"))
    parser.add_argument("--serif", type=Path, default=Path("C:/Windows/Fonts/NotoSerifSC-VF.ttf"))
    args = parser.parse_args()
    target = ROOT / "bank_agent/static/fonts"
    target.mkdir(exist_ok=True)
    common = set(range(32, 127)) | set(range(0x3000, 0x3040)) | set(range(0xFF00, 0xFFF0))
    for code in range(0x4E00, 0xA000):
        try:
            chr(code).encode("gb2312")
        except UnicodeEncodeError:
            continue
        common.add(code)
    for path in (ROOT / "bank_agent").rglob("*"):
        if path.suffix in {".py", ".js", ".html"}:
            common.update(map(ord, path.read_text(encoding="utf-8")))
    display = set(
        map(
            ord,
            "简行一句话，简化银行办理。个人工作台今日事务有度有序有据余说说你的安排办理进度收支与理财研究流览盈守续序慎从容",
        )
    ) | set(range(32, 127))
    manifest = []
    for source, family, filename, codes, kind in [
        (args.sans, "Jianxing Text SC", "jianxing-text-sc.woff2", common, "Sans"),
        (args.serif, "Jianxing Display SC", "jianxing-display-sc.woff2", display, "Serif"),
    ]:
        font = TTFont(source)
        assert "SIL Open Font License" in font["name"].getDebugName(13), "Only the verified OFL fonts may be packaged"
        for record in list(font["name"].names):
            names = {
                1: family,
                2: "Regular",
                3: family + " Workspace Subset",
                4: family,
                6: family.replace(" ", ""),
                16: family,
                17: "Regular",
            }
            if record.nameID in names:
                font["name"].setName(
                    names[record.nameID], record.nameID, record.platformID, record.platEncID, record.langID
                )
        options = subset.Options()
        options.flavor = "woff2"
        options.layout_features = ["*"]
        options.name_IDs = ["*"]
        sub = subset.Subsetter(options=options)
        sub.populate(unicodes=codes)
        sub.subset(font)
        font.flavor = "woff2"
        font.save(target / filename)
        url = f"https://raw.githubusercontent.com/notofonts/noto-cjk/main/{kind}/LICENSE"
        response = httpx.get(url, timeout=30)
        response.raise_for_status()
        (target / (kind + "-OFL.txt")).write_text(response.text, encoding="utf-8")
        manifest.append(
            {
                "family": family,
                "original_family": "Noto " + kind + " SC",
                "file": filename,
                "bytes": (target / filename).stat().st_size,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "license_source": url,
            }
        )
        print(filename, (target / filename).stat().st_size, flush=True)
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
