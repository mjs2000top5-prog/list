import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import time
import io
import re

st.set_page_config(page_title="세무사 정보 크롤러", page_icon="🔍", layout="wide")

st.markdown("""
<style>
.region-chip {
    display:inline-block; background:#eff6ff; color:#1d4ed8;
    border:1px solid #bfdbfe; padding:3px 10px; border-radius:20px;
    margin:2px; font-size:13px;
}
</style>
""", unsafe_allow_html=True)


# ── Google Sheets 연결 ─────────────────────────────────────────────────────────
@st.cache_resource
def get_gspread_client():
    creds_dict = st.secrets["gcp_service_account"]
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(dict(creds_dict), scopes=scopes)
    return gspread.authorize(creds)


def get_spreadsheet(client, sheet_id):
    return client.open_by_key(sheet_id)


# ── URL 목록 로드 ──────────────────────────────────────────────────────────────
def load_url_list(spreadsheet):
    try:
        ws = spreadsheet.worksheet("리스트")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.get_worksheet(0)

    rows = ws.get_all_values()
    if not rows:
        return pd.DataFrame(columns=["지역", "상세지역", "URL"])

    headers = rows[0]

    def find_col(keywords):
        for i, h in enumerate(headers):
            if any(k in h for k in keywords):
                return i
        return None

    i_region = find_col(["지역"])
    i_detail = find_col(["상세"])
    i_url    = find_col(["URL", "url", "주소"])

    if i_url is None:
        st.error("URL 컬럼을 찾을 수 없습니다.")
        return pd.DataFrame()

    records = []
    for row in rows[1:]:
        if len(row) <= i_url or not row[i_url].strip():
            continue
        records.append({
            "지역":   row[i_region].strip() if i_region is not None else "",
            "상세지역": row[i_detail].strip() if i_detail is not None else "",
            "URL":   row[i_url].strip(),
        })
    return pd.DataFrame(records)


# ── 크롤링 ────────────────────────────────────────────────────────────────────
PHONE_RE = re.compile(r'0\d{1,2}-\d{3,4}-\d{4}')
NAME_RE  = re.compile(r'^[가-힣]{2,4}$')


def parse_row(tds: list) -> dict | None:
    name = office = phone = status = ""
    for td in tds:
        text = td.get_text(" ", strip=True)
        if PHONE_RE.search(text) and not phone:
            phone = PHONE_RE.search(text).group()
        elif NAME_RE.match(text) and not name:
            name = text
        elif any(kw in text for kw in ["세무", "회계", "사무소", "법인"]) and not office:
            office = text
        elif text in ("개업", "폐업", "휴업") and not status:
            status = text
    return {"이름": name, "전화번호": phone, "사무소명": office, "현황": status or "개업"} if name and phone else None


def crawl_one(region, detail, url, session) -> list:
    results = []
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": "https://www.kacta.or.kr/",
        }
        resp = session.get(url, headers=headers, timeout=20)
        resp.encoding = "euc-kr"
        if resp.status_code != 200:
            return results

        soup = BeautifulSoup(resp.text, "html.parser")
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            has_data = False
            for row in rows:
                tds = row.find_all("td")
                if len(tds) < 4:
                    continue
                parsed = parse_row(tds)
                if parsed:
                    has_data = True
                    results.append({"지역": region, "상세지역": detail, **parsed, "URL": url})
            if has_data:
                break
    except Exception as e:
        st.warning(f"⚠️ {region} {detail}: {type(e).__name__}")
    return results


def crawl_all(url_df, prog, status_text) -> pd.DataFrame:
    session = requests.Session()
    try:
        session.get("https://www.kacta.or.kr/", timeout=10)
    except Exception:
        pass

    all_data = []
    total = len(url_df)
    for idx, (_, row) in enumerate(url_df.iterrows()):
        status_text.text(f"🔄 {row['지역']} {row['상세지역']} ({idx+1}/{total})")
        prog.progress((idx + 1) / total)
        all_data.extend(crawl_one(row["지역"], row["상세지역"], row["URL"], session))
        time.sleep(0.35)

    cols = ["지역", "상세지역", "이름", "전화번호", "사무소명", "현황", "URL"]
    return pd.DataFrame(all_data) if all_data else pd.DataFrame(columns=cols)


# ── 시트 업로드 ───────────────────────────────────────────────────────────────
def upload_to_sheet(spreadsheet, df, sheet_name):
    try:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=len(df)+5, cols=len(df.columns)+1)
    except gspread.exceptions.APIError:
        ws = spreadsheet.worksheet(sheet_name)
        ws.clear()

    data = [df.columns.tolist()] + df.fillna("").values.tolist()
    ws.update(data, value_input_option="RAW")
    ws.format(f"A1:{chr(64+len(df.columns))}1", {
        "textFormat": {"bold": True, "foregroundColor": {"red":1,"green":1,"blue":1}},
        "backgroundColor": {"red":0.13,"green":0.25,"blue":0.67},
        "horizontalAlignment": "CENTER",
    })
    return ws


# ── 이전 데이터 로드 ──────────────────────────────────────────────────────────
def load_previous_data(spreadsheet):
    date_sheets = sorted(
        [ws.title for ws in spreadsheet.worksheets() if re.match(r"\d{4}-\d{2}-\d{2}", ws.title)]
    )
    if len(date_sheets) < 2:
        return None, (date_sheets[-1] if date_sheets else None)
    prev_title = date_sheets[-2]
    records = spreadsheet.worksheet(prev_title).get_all_records()
    return pd.DataFrame(records), prev_title


# ── 신규 비교 ─────────────────────────────────────────────────────────────────
KEY_COLS = ["지역", "상세지역", "이름", "전화번호"]


def find_new_entries(current_df, previous_df):
    if previous_df is None or previous_df.empty:
        return current_df.copy()
    prev_set = set(
        previous_df.reindex(columns=KEY_COLS).fillna("").apply(lambda r: tuple(r.astype(str)), axis=1)
    )
    mask = current_df.reindex(columns=KEY_COLS).fillna("").apply(
        lambda r: tuple(r.astype(str)) not in prev_set, axis=1
    )
    return current_df[mask].copy()


# ── UI ────────────────────────────────────────────────────────────────────────
st.title("🔍 세무사 정보 크롤러")
st.caption("kacta.or.kr 지역별 세무사 이름·전화번호·현황 수집 → Google Sheets 날짜별 저장 + 신규 비교")

with st.sidebar:
    st.header("⚙️ 설정")
    sheet_id = st.text_input(
        "Google Sheets ID",
        value="18ld7dK3aAmJljJRNTtF3_oWi-M007hr9F_FHfYgl0vc",
    )
    st.markdown("---")
    st.markdown("""**📋 동작 순서**
1. `리스트` 시트 URL 읽기
2. 전 지역 크롤링
3. `YYYY-MM-DD` 시트 자동 생성
4. 직전 시트 대비 신규 표시
5. CSV / Excel 다운로드""")

col1, col2 = st.columns([3, 1])
with col1:
    run_btn = st.button("🚀 크롤링 시작", type="primary", use_container_width=True)
with col2:
    preview_btn = st.button("👁️ URL 미리보기", use_container_width=True)

if preview_btn:
    with st.spinner("연결 중..."):
        try:
            client = get_gspread_client()
            sp = get_spreadsheet(client, sheet_id)
            url_df = load_url_list(sp)
            st.success(f"총 **{len(url_df)}**개 지역 URL 확인")
            c1, c2 = st.columns([1, 2])
            c1.dataframe(url_df["지역"].value_counts().reset_index().rename(columns={"지역":"지역","count":"구 수"}))
            c2.dataframe(url_df, use_container_width=True, height=380)
        except Exception as e:
            st.error(f"오류: {e}")

if run_btn:
    try:
        client = get_gspread_client()
        sp = get_spreadsheet(client, sheet_id)

        with st.spinner("URL 로드 중..."):
            url_df = load_url_list(sp)
        if url_df.empty:
            st.error("URL 목록이 비어 있습니다.")
            st.stop()

        st.info(f"총 **{len(url_df)}**개 지역 크롤링 시작")
        st.subheader("📡 크롤링 진행")
        prog = st.progress(0)
        status = st.empty()
        today = datetime.now().strftime("%Y-%m-%d")

        current_df = crawl_all(url_df, prog, status)
        status.text(f"✅ 완료 — {len(current_df):,}건 수집")

        if current_df.empty:
            st.error("수집된 데이터가 없습니다.")
            st.stop()

        with st.spinner(f"'{today}' 시트 생성 중..."):
            upload_to_sheet(sp, current_df, today)
        st.success(f"✅ '{today}' 시트 저장 ({len(current_df):,}건)")

        with st.spinner("이전 데이터 비교 중..."):
            previous_df, prev_title = load_previous_data(sp)
        new_df = find_new_entries(current_df, previous_df)

        # 요약
        st.markdown("---")
        st.subheader("📊 결과 요약")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("총 수집", f"{len(current_df):,}건")
        m2.metric("🆕 신규", f"{len(new_df):,}건", delta=f"+{len(new_df)}" if new_df is not None else "0")
        m3.metric("비교 기준 시트", prev_title or "없음")
        m4.metric("수집 지역", f"{current_df['지역'].nunique()}개")

        # 신규 지역별 출력
        if not new_df.empty:
            st.markdown("---")
            st.subheader(f"🆕 신규 등록 세무사 — {len(new_df):,}건")

            regions_new = new_df["지역"].unique().tolist()
            if len(regions_new) <= 12:
                tabs = st.tabs(regions_new)
                for tab, region in zip(tabs, regions_new):
                    with tab:
                        rdf = new_df[new_df["지역"] == region]
                        for detail in rdf["상세지역"].unique():
                            ddf = rdf[rdf["상세지역"] == detail].drop(columns=["URL","지역"], errors="ignore").reset_index(drop=True)
                            st.markdown(f"**📍 {detail}** — {len(ddf)}건")
                            st.dataframe(ddf, use_container_width=True)
            else:
                st.dataframe(new_df.drop(columns=["URL"], errors="ignore").reset_index(drop=True),
                             use_container_width=True, height=500)

            # 다운로드
            st.markdown("---")
            st.subheader("⬇️ 다운로드")
            dc1, dc2, dc3 = st.columns(3)
            dc1.download_button("📥 신규 CSV", new_df.to_csv(index=False, encoding="utf-8-sig"),
                                 f"신규세무사_{today}.csv", "text/csv", use_container_width=True)
            dc2.download_button("📥 전체 CSV", current_df.to_csv(index=False, encoding="utf-8-sig"),
                                 f"전체세무사_{today}.csv", "text/csv", use_container_width=True)
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as w:
                new_df.to_excel(w, sheet_name="신규등록", index=False)
                current_df.to_excel(w, sheet_name="전체데이터", index=False)
            dc3.download_button("📥 Excel (신규+전체)", buf.getvalue(),
                                 f"세무사_{today}.xlsx",
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 use_container_width=True)
        else:
            if previous_df is not None:
                st.success("✅ 신규 등록 데이터 없음")
            else:
                st.info("📌 첫 조회입니다. 다음 조회부터 신규 비교가 가능합니다.")
            st.download_button("📥 전체 CSV", current_df.to_csv(index=False, encoding="utf-8-sig"),
                                f"전체세무사_{today}.csv", "text/csv")

    except Exception as e:
        st.error(f"❌ 오류: {e}")
        st.exception(e)


# ── 이전 기록 조회 ────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📅 이전 조회 기록")

try:
    client = get_gspread_client()
    sp = get_spreadsheet(client, sheet_id)
    date_sheets = sorted(
        [ws.title for ws in sp.worksheets() if re.match(r"\d{4}-\d{2}-\d{2}", ws.title)],
        reverse=True
    )
    if date_sheets:
        selected = st.selectbox("날짜 선택", date_sheets)
        if st.button("📂 불러오기"):
            hist_df = pd.DataFrame(sp.worksheet(selected).get_all_records())
            st.dataframe(hist_df, use_container_width=True, height=400)
            c1, c2 = st.columns(2)
            c1.download_button(f"📥 {selected} CSV",
                                hist_df.to_csv(index=False, encoding="utf-8-sig"),
                                f"세무사_{selected}.csv", "text/csv", use_container_width=True)
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as w:
                hist_df.to_excel(w, index=False)
            c2.download_button(f"📥 {selected} Excel", buf.getvalue(),
                                f"세무사_{selected}.xlsx",
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                use_container_width=True)
    else:
        st.caption("아직 조회 기록이 없습니다.")
except Exception as e:
    st.caption(f"기록 로드 실패: {e}")
