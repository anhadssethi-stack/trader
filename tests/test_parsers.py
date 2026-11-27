"""Parsers for each NSE file format, using small samples in the real layouts."""
import io
import zipfile
from datetime import date

from nse_research.data.sources.nse import (NseFullBhavcopy, NseIndexClose, NseLegacyBhavcopy, NseLists,
                                           NseMto, NseUdiffBhavcopy)

UDIFF = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
    "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
    "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
    "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
    "2026-11-26,2026-11-26,CM,NSE,STK,2885,INE002A01018,RELIANCE,EQ,,,,,RELIANCE INDUSTRIES LTD,"
    "1290.00,1305.50,1285.10,1301.20,1301.00,1288.40,,1301.20,,,8123456,10567890123.45,234567,F1,1,,,,,\n"
    "2026-11-26,2026-11-26,CM,NSE,STK,1594,INE009A01021,INFY,EQ,,,,,INFOSYS LIMITED,"
    "1800,1810,1790,1805,1805,1799,,1805,,,4000000,7220000000,100000,F1,1,,,,,\n"
)

LEGACY = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,\n"
    "RELIANCE,EQ,2500,2520,2490,2510,2511,2495,5000000,12550000000,05-JUL-2024,150000,INE002A01018,\n"
    "SOMEBE,BE,10,11,9.5,10.5,10.5,10,1000,10500,05-JUL-2024,20,INE000000001,\n"
)

FULL = (
    "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, "
    "AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
    "RELIANCE, EQ, 26-Nov-2026, 1288.40, 1290.00, 1305.50, 1285.10, 1301.00, 1301.20, 1298.11, "
    "8123456, 105678.90, 234567, 4061728, 50.00\n"
    "SGBJAN29, GB, 26-Nov-2026, 6000, 6000, 6000, 6000, 6000, 6000, 6000, 10, 0.6, 2, -, -\n"
)

MTO = (
    "Security Wise Delivery Position - Compulsory Rolling Settlement\n"
    "10,MTO,26112026,1234567,0001234\n"
    "Trade Date <26-NOV-2026>,Settlement Type <N>\n"
    "Record Type,Sr No,Name of Security,series,Quantity Traded,Deliverable Quantity,% of Deliverable\n"
    "20,1,RELIANCE,EQ,8123456,4061728,50.00\n"
    "20,2,INFY,EQ,4000000,2600000,65.00\n"
)

INDEX = (
    "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,"
    "Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n"
    "Nifty 50,26-11-2026,24000.1,24100.5,23950.2,24080.3,80.2,0.33,300000000,25000,22.1,3.6,1.2\n"
    "Nifty 500,26-11-2026,22000,22100,21950,22050.5,50.5,0.23,600000000,60000,23.0,3.9,1.1\n"
)

N500 = ("Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018\n"
        "Infosys Ltd.,Information Technology,INFY,EQ,INE009A01021\n")

FNO = ("UNDERLYING                    ,SYMBOL    ,NOV-26,DEC-26,JAN-27\n"
       "NIFTY 50                      ,NIFTY     ,75,75,75\n"
       "Symbol,SYMBOL,,,\n"
       "RELIANCE INDUSTRIES LTD       ,RELIANCE  ,500,500,500\n")


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("file.csv", text)
    return buf.getvalue()


def test_udiff_parser_reads_zip_and_maps_columns():
    df = NseUdiffBhavcopy.parse(_zip(UDIFF))
    r = df[df.symbol == "RELIANCE"].iloc[0]
    assert r.date == date(2026, 11, 26)
    assert r.close == 1301.20 and r.prev_close == 1288.40 and r.volume == 8123456
    assert r["isin"] == "INE002A01018" and r["source"] == "nse_udiff"
    assert len(df) == 2


def test_udiff_url():
    assert NseUdiffBhavcopy.url(date(2026, 11, 26)).endswith("BhavCopy_NSE_CM_0_0_0_20261126_F_0000.csv.zip")


def test_legacy_parser_and_url():
    df = NseLegacyBhavcopy.parse(_zip(LEGACY))
    assert set(df.series) == {"EQ", "BE"}
    assert df.iloc[0].date == date(2024, 7, 5) and df.iloc[0].prev_close == 2495
    assert NseLegacyBhavcopy.url(date(2016, 3, 1)).endswith("/2016/MAR/cm01MAR2016bhav.csv.zip")


def test_full_bhavcopy_prices_and_delivery_handle_padding_and_dashes():
    raw = FULL.encode()
    px = NseFullBhavcopy.parse_prices(raw)
    assert px.iloc[0].symbol == "RELIANCE" and px.iloc[0].turnover == 105678.90 * 1e5
    dl = NseFullBhavcopy.parse_delivery(raw)
    assert list(dl.symbol) == ["RELIANCE"]  # "-" rows dropped
    assert dl.iloc[0].deliv_pct == 50.0


def test_mto_parser_keeps_only_security_rows():
    dl = NseMto.parse(MTO.encode(), date(2026, 11, 26))
    assert list(dl.symbol) == ["RELIANCE", "INFY"]
    assert dl.iloc[1].deliv_pct == 65.0


def test_index_close_parser():
    df = NseIndexClose.parse(INDEX.encode())
    r = df[df.index_name == "Nifty 500"].iloc[0]
    assert r.close == 22050.5 and r.date == date(2026, 11, 26)


def test_lists():
    u = NseLists.parse_constituents(N500.encode())
    assert list(u.symbol) == ["RELIANCE", "INFY"] and u.iloc[1].industry == "Information Technology"
    f = NseLists.parse_fno(FNO.encode())
    assert "RELIANCE" in set(f.symbol) and "Symbol" not in set(f.symbol)
