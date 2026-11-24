"""Announcement times of Upbit / Bithumb KRW listings, from Blockmedia's news feed (public WordPress API).

Blockmedia posts "업비트, X 원화 마켓 상장" within minutes of the exchange notice. For each listing in
results/listings.csv, the earliest article in the 3 days before trading opens whose title names the exchange, the
ticker and a listing is taken as the announcement time. Since 2026 headlines give only the Korean name, a listing
headline whose article text names the ticker also counts. Saves results/krw_announcements.csv.
"""

import re
import time
from pathlib import Path

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
API = "https://www.blockmedia.co.kr/wp-json/wp/v2/posts"
NAME = {"upbit": "업비트", "bithumb": "빗썸"}


def articles(query, after, before):
    r = requests.get(API, params={"search": query, "after": after.strftime("%Y-%m-%dT%H:%M:%S"),
                                  "before": before.strftime("%Y-%m-%dT%H:%M:%S"), "per_page": 50,
                                  "_fields": "date_gmt,title,link,content"},
                     headers={"User-Agent": "research-script"}, timeout=30)
    r.raise_for_status()
    return [(pd.Timestamp(a["date_gmt"], tz="UTC"), re.sub("<[^>]+>", "", a["title"]["rendered"]), a["link"],
             re.sub("<[^>]+>", " ", a["content"]["rendered"])) for a in r.json()]


def main() -> None:
    ev = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    ev["listed"] = pd.to_datetime(ev.listed, utc=True)
    ev = ev[ev.venue.isin(NAME)].reset_index(drop=True)
    rows = []
    for r in ev.itertuples():
        ex = NAME[r.venue]
        # the feed's after/before filters use site time (KST)
        a = (r.listed - pd.Timedelta(days=3)).tz_convert("Asia/Seoul").tz_localize(None)
        b = (r.listed + pd.Timedelta(hours=1)).tz_convert("Asia/Seoul").tz_localize(None)
        hit = None
        for q in (f"{ex} {r.base}", f"{ex} 상장"):
            arts = articles(q, a, b)
            time.sleep(1)
            tick = re.compile(rf"(?<![A-Za-z0-9]){r.base}(?![A-Za-z0-9])")
            heads = [x for x in arts if ex in x[1] and "상장" in x[1] and not x[1].startswith("[")]
            ok = ([x[:3] for x in heads if tick.search(x[1])]
                  or [x[:3] for x in heads if f"({r.base})" in x[3] or f"({r.base}" in x[3]])
            if ok:
                hit = min(ok)
                break
        rows.append({"venue": r.venue, "base": r.base, "perp": r.perp, "listed": r.listed,
                     "announced": hit[0] if hit else pd.NaT, "title": hit[1] if hit else "", "link": hit[2] if hit else ""})
        print(r.venue, r.base, r.listed, "->", hit[0] if hit else "not found", flush=True)
    out = pd.DataFrame(rows)
    out.to_csv(HERE / "results" / "krw_announcements.csv", index=False)
    f = out.announced.notna()
    lag = (out.listed - out.announced)[f] / pd.Timedelta(hours=1)
    print(f"found {f.sum()} of {len(out)}; hours from article to trading: median {lag.median():.1f}, "
          f"quartiles {lag.quantile(0.25):.1f}-{lag.quantile(0.75):.1f}, negative {int((lag < 0).sum())}")


if __name__ == "__main__":
    main()
