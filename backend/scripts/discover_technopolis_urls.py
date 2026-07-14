import asyncio
import re
import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
PDP = re.compile(r"https://www\.technopolis\.bg/bg/[^\"'\s<>]+/p/\d+", re.I)


async def main() -> None:
    urls: list[str] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as c:
        for path in (
            "https://www.technopolis.bg/sitemap.xml",
            "https://www.technopolis.bg/bg/sitemap.xml",
        ):
            try:
                r = await c.get(path)
                print(path, r.status_code, len(r.text))
                for m in PDP.findall(r.text):
                    if m not in urls:
                        urls.append(m)
            except Exception as exc:
                print(path, "err", exc)
        seeds = [
            "https://www.technopolis.bg/bg/Smartfoni-i-Nosimi-Ustroistva/Smartfoni",
            "https://www.technopolis.bg/bg/Kompyutri-i-Tableti/Laptopi",
            "https://www.technopolis.bg/bg/Televizori-i-Audio/Televizori",
            "https://www.technopolis.bg/bg/Dom-i-Ofis/Prahosmukachki",
            "https://www.technopolis.bg/bg/Gaming/Gaming-Konsoli",
        ]
        for s in seeds:
            if len(urls) >= 8:
                break
            r = await c.get(s, headers={"Accept-Language": "bg-BG"})
            print("seed", s, r.status_code, len(r.text))
            for m in PDP.findall(r.text):
                if m not in urls:
                    urls.append(m)
    for u in urls[:10]:
        print(u)


if __name__ == "__main__":
    asyncio.run(main())
