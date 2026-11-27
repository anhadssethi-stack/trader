"""NSE end-of-day files: bhavcopy (two formats), full bhavcopy with delivery, MTO
delivery files, index closes, the Nifty 500 list and the F&O list.

All of these are public archive files, but NSE has no official API for scripts.
Known failure modes are handled by the caller (pipeline.py): 404 on holidays or
before publication (~6-7 PM IST), 403 when NSE blocks the client/IP, HTML error
pages served with status 200, and occasional format changes (e.g. July 2024).
"""
from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta

import pandas as pd

from ..http import HttpClient
from .base import (DELIVERY_COLS, INDEX_COLS, PRICE_COLS, finish, normalise_columns, to_num)

ARCHIVE = "https://nsearchives.nseindia.com"
UDIFF_START = date(2024, 7, 8)  # first day of the UDiFF bhavcopy format


def _read_csv_bytes(data: bytes) -> pd.DataFrame:
    if data[:2] == b"PK":  # zip archive
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            data = z.read(name)
    return normalise_columns(pd.read_csv(io.BytesIO(data), dtype=str, skipinitialspace=True))


# ---------------------------------------------------------------- price sources
class NseUdiffBhavcopy:
    """CM bhavcopy in the UDiFF format (8 July 2024 onwards)."""
    name = "nse_udiff"
    kind = "price"

    def __init__(self, http: HttpClient):
        self.http = http

    @staticmethod
    def url(d: date) -> str:
        return f"{ARCHIVE}/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"

    def supports(self, d: date) -> bool:
        return d >= UDIFF_START

    def fetch_day(self, d: date) -> pd.DataFrame:
        raw = self.http.get_cached(self.url(d), f"nse/udiff/{d:%Y}/{d:%Y%m%d}.csv.zip")
        return self.parse(raw)

    @staticmethod
    def parse(raw: bytes) -> pd.DataFrame:
        df = _read_csv_bytes(raw)
        if "FinInstrmTp" in df.columns:
            df = df[df["FinInstrmTp"].isin(["STK"])]
        out = pd.DataFrame({
            "date": pd.to_datetime(df["TradDt"]),
            "symbol": df["TckrSymb"],
            "series": df["SctySrs"],
            "isin": df["ISIN"],
            "open": to_num(df["OpnPric"]),
            "high": to_num(df["HghPric"]),
            "low": to_num(df["LwPric"]),
            "close": to_num(df["ClsPric"]),
            "prev_close": to_num(df["PrvsClsgPric"]),
            "volume": to_num(df["TtlTradgVol"]),
            "turnover": to_num(df["TtlTrfVal"]),
            "trades": to_num(df["TtlNbOfTxsExctd"]),
            "source": "nse_udiff",
        })
        return finish(out, PRICE_COLS)


class NseLegacyBhavcopy:
    """Old-format CM bhavcopy (cmDDMONYYYYbhav.csv.zip), used for history before July 2024."""
    name = "nse_legacy"
    kind = "price"

    def __init__(self, http: HttpClient):
        self.http = http

    @staticmethod
    def url(d: date) -> str:
        mon = d.strftime("%b").upper()
        return f"{ARCHIVE}/content/historical/EQUITIES/{d:%Y}/{mon}/cm{d:%d}{mon}{d:%Y}bhav.csv.zip"

    def supports(self, d: date) -> bool:
        return d < UDIFF_START + timedelta(days=30)  # small overlap in case the old file lingers

    def fetch_day(self, d: date) -> pd.DataFrame:
        raw = self.http.get_cached(self.url(d), f"nse/legacy/{d:%Y}/{d:%Y%m%d}.csv.zip")
        return self.parse(raw)

    @staticmethod
    def parse(raw: bytes) -> pd.DataFrame:
        df = _read_csv_bytes(raw)
        out = pd.DataFrame({
            "date": pd.to_datetime(df["TIMESTAMP"], format="%d-%b-%Y"),
            "symbol": df["SYMBOL"],
            "series": df["SERIES"],
            "isin": df.get("ISIN"),
            "open": to_num(df["OPEN"]),
            "high": to_num(df["HIGH"]),
            "low": to_num(df["LOW"]),
            "close": to_num(df["CLOSE"]),
            "prev_close": to_num(df["PREVCLOSE"]),
            "volume": to_num(df["TOTTRDQTY"]),
            "turnover": to_num(df["TOTTRDVAL"]),
            "trades": to_num(df["TOTALTRADES"]) if "TOTALTRADES" in df else None,
            "source": "nse_legacy",
        })
        return finish(out, PRICE_COLS)


class NseFullBhavcopy:
    """sec_bhavdata_full: prices *and* delivery in one file. Serves as a price
    fallback and as the primary delivery source."""
    name = "nse_full_bhav"
    kind = "price+delivery"

    def __init__(self, http: HttpClient):
        self.http = http

    @staticmethod
    def url(d: date) -> str:
        return f"{ARCHIVE}/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"

    def supports(self, d: date) -> bool:
        return True

    def fetch_day(self, d: date) -> pd.DataFrame:
        return self.parse_prices(self._frame_raw(d))

    def fetch_delivery(self, d: date) -> pd.DataFrame:
        return self.parse_delivery(self._frame_raw(d))

    def _frame_raw(self, d: date) -> bytes:
        return self.http.get_cached(self.url(d), f"nse/full/{d:%Y}/{d:%Y%m%d}.csv")

    @staticmethod
    def parse_prices(raw: bytes) -> pd.DataFrame:
        df = _read_csv_bytes(raw)
        out = pd.DataFrame({
            "date": pd.to_datetime(df["DATE1"], format="%d-%b-%Y"),
            "symbol": df["SYMBOL"],
            "series": df["SERIES"],
            "isin": None,
            "open": to_num(df["OPEN_PRICE"]),
            "high": to_num(df["HIGH_PRICE"]),
            "low": to_num(df["LOW_PRICE"]),
            "close": to_num(df["CLOSE_PRICE"]),
            "prev_close": to_num(df["PREV_CLOSE"]),
            "volume": to_num(df["TTL_TRD_QNTY"]),
            "turnover": to_num(df["TURNOVER_LACS"]) * 1e5,
            "trades": to_num(df["NO_OF_TRADES"]),
            "source": "nse_full_bhav",
        })
        return finish(out, PRICE_COLS)

    @staticmethod
    def parse_delivery(raw: bytes) -> pd.DataFrame:
        df = _read_csv_bytes(raw)
        out = pd.DataFrame({
            "date": pd.to_datetime(df["DATE1"], format="%d-%b-%Y"),
            "symbol": df["SYMBOL"],
            "series": df["SERIES"],
            "deliv_qty": to_num(df["DELIV_QTY"]),
            "deliv_pct": to_num(df["DELIV_PER"]),
            "source": "nse_full_bhav",
        })
        out = out.dropna(subset=["deliv_pct"])
        return finish(out, DELIVERY_COLS)


class NseMto:
    """MTO_DDMMYYYY.DAT security-wise delivery file (long history, delivery fallback)."""
    name = "nse_mto"
    kind = "delivery"

    def __init__(self, http: HttpClient):
        self.http = http

    @staticmethod
    def url(d: date) -> str:
        return f"{ARCHIVE}/archives/equities/mto/MTO_{d:%d%m%Y}.DAT"

    def fetch_delivery(self, d: date) -> pd.DataFrame:
        raw = self.http.get_cached(self.url(d), f"nse/mto/{d:%Y}/{d:%Y%m%d}.DAT")
        return self.parse(raw, d)

    @staticmethod
    def parse(raw: bytes, d: date) -> pd.DataFrame:
        rows = []
        for line in raw.decode("utf-8", errors="ignore").splitlines():
            parts = [p.strip() for p in line.split(",")]
            # Record type 20 = security row: 20,SrNo,Symbol,Series,TradedQty,DelivQty,Deliv%
            if len(parts) >= 7 and parts[0] == "20":
                rows.append(parts[:7])
        df = pd.DataFrame(rows, columns=["rt", "sr", "symbol", "series", "qty", "deliv_qty", "deliv_pct"])
        out = pd.DataFrame({
            "date": pd.Timestamp(d),
            "symbol": df["symbol"],
            "series": df["series"],
            "deliv_qty": to_num(df["deliv_qty"]),
            "deliv_pct": to_num(df["deliv_pct"]),
            "source": "nse_mto",
        })
        return finish(out, DELIVERY_COLS)


# ---------------------------------------------------------------- indices
class NseIndexClose:
    """ind_close_all_DDMMYYYY.csv: closing values of every NSE index for a day."""
    name = "nse_index_close"
    kind = "index"

    def __init__(self, http: HttpClient):
        self.http = http

    @staticmethod
    def url(d: date) -> str:
        return f"{ARCHIVE}/content/indices/ind_close_all_{d:%d%m%Y}.csv"

    def fetch_day(self, d: date) -> pd.DataFrame:
        raw = self.http.get_cached(self.url(d), f"nse/index/{d:%Y}/{d:%Y%m%d}.csv")
        return self.parse(raw)

    @staticmethod
    def parse(raw: bytes) -> pd.DataFrame:
        df = _read_csv_bytes(raw)
        out = pd.DataFrame({
            "date": pd.to_datetime(df["Index Date"], dayfirst=True),
            "index_name": df["Index Name"],
            "open": to_num(df["Open Index Value"]),
            "high": to_num(df["High Index Value"]),
            "low": to_num(df["Low Index Value"]),
            "close": to_num(df["Closing Index Value"]),
            "source": "nse_index_close",
        })
        return finish(out, INDEX_COLS)


# ---------------------------------------------------------------- lists
class NseLists:
    """Index constituents and the F&O list. Only the *current* lists are public."""

    INDEX_URLS = {
        "nifty500": [
            f"{ARCHIVE}/content/indices/ind_nifty500list.csv",
            "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
        ],
    }
    FNO_URL = f"{ARCHIVE}/content/fo/fo_mktlots.csv"

    def __init__(self, http: HttpClient):
        self.http = http

    def index_constituents(self, index: str = "nifty500") -> pd.DataFrame:
        last: Exception | None = None
        for url in self.INDEX_URLS[index]:
            try:
                raw = self.http.get_cached(url, f"nse/lists/{index}_{date.today():%Y%m%d}.csv")
                return self.parse_constituents(raw)
            except Exception as e:  # noqa: BLE001 - try the next mirror
                last = e
        raise last  # type: ignore[misc]

    @staticmethod
    def parse_constituents(raw: bytes) -> pd.DataFrame:
        df = _read_csv_bytes(raw)
        return pd.DataFrame({
            "symbol": df["Symbol"],
            "company": df["Company Name"],
            "industry": df["Industry"],
            "series": df["Series"],
            "isin": df["ISIN Code"],
        })

    def fno_symbols(self) -> pd.DataFrame:
        raw = self.http.get_cached(self.FNO_URL, f"nse/lists/fno_{date.today():%Y%m%d}.csv")
        return self.parse_fno(raw)

    @staticmethod
    def parse_fno(raw: bytes) -> pd.DataFrame:
        df = _read_csv_bytes(raw)
        sym_col = next(c for c in df.columns if c.upper() == "SYMBOL")
        syms = df[sym_col].astype(str).str.strip()
        syms = syms[(syms != "") & (syms.str.upper() != "SYMBOL")]
        # Index derivatives (NIFTY, BANKNIFTY, ...) are in the same file; the caller
        # intersects with the equity universe, so they drop out naturally.
        return pd.DataFrame({"symbol": syms.unique()})
