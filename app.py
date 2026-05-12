"""
개업 세무사 조회
출력 형태: 지역 / 상세지역 / 이름 / 전화번호 / 현황
"""

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

# ── 페이지 설정 ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="개업 세무사 조회", page_icon="🔍", layout="wide")

st.markdown("""
<style>
[data-testid="stMetricValue"] { font-size: 1.8rem; font-weight: 700; }
.section-title { font-size: 1.1rem; font-weight: 700; color: #1e3a8a; margin: 4px 0; }
thead tr th { background: #1e3a8a !important; color: white !important; }
</style>
""", unsafe_allow_html=True)


# ── Google Sheets 연결 ───────────────────────────────────────────────────────
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


# ── URL 목록 로드 ─────────────────────────────────────────────────────────────
def load_url_list(spreadsheet) -> pd.DataFrame:
    """리스트 시트에서 지역 / 상세지역 / URL 읽기"""
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
    i_url    = find_col(["URL", "url"])

    if i_url is None:
        st.error("URL 컬럼을 찾을 수 없습니다.")
        return pd.DataFrame()

    records = []
    for row in rows[1:]:
        if len(row) <= i_url or not row[i_url].strip():
            continue
        records.append({
            "지역":    row[i_region].strip() if i_region is not None else "",
            "상세지역": row[i_detail].strip() if i_detail is not None else "",
            "URL":    row[i_url].strip(),
        })
    return pd.DataFrame(records)


# ── kacta.or.kr 파서 ─────────────────────────────────────────────────────────
NAME_RE     = re.compile(r'^[가-힣]{2,5}$')
PHONE_RE    = re.compile(r'^(0\d{1,2}[-\s]\d{3,4}[-\s]\d{4}|비공개|[-\d\s/]+)$')
STATUS_VALS = {"개업", "폐업", "휴업"}


def is_phone(text: str) -> bool:
    """전화번호 또는 비공개인지 판별"""
    if text == "비공개":
        return True
    # 숫자/하이픈/슬래시로 이뤄진 전화번호 패턴
    return bool(re.match(r'^0\d', text)) and bool(re.search(r'\d{4}', text))


def parse_kacta_table(soup: BeautifulSoup) -> list[dict]:
    """
    kacta.or.kr 테이블 구조 파서.
    반환: [{"이름": ..., "전화번호": ..., "현황": ...}, ...]
    """
    results = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        parsed_in_table = []

        for row in rows:
            tds = row.find_all("td")
            if len(tds) < 3:
                continue

            texts = [td.get_text(" ", strip=True) for td in tds]

            name    = ""
            phone   = ""
            status  = ""

            for text in texts:
                if not text:
                    continue
                if not name and NAME_RE.match(text):
                    name = text
                elif not phone and is_phone(text):
                    phone = text
                elif not status and text in STATUS_VALS:
                    status = text

            if name:
                parsed_in_table.append({
                    "이름":    name,
                    "전화번호": phone if phone else "비공개",
                    "현황":    status if status else "개업",
                })

        # 데이터가 있는 첫 번째 테이블만 사용
        if parsed_in_table:
            results = parsed_in_table
            break

    return results


def crawl_one(region: str, detail: str, url: str, session: requests.Session) -> list[dict]:
    """단일 URL 크롤링 → 지역/상세지역 붙여서 반환"""
    rows = []
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.kacta.or.kr/",
            "Connection": "keep-alive",
        }
        resp = session.get(url, headers=headers, timeout=25)
        if resp.status_code != 200:
            return rows
        resp.encoding = "euc-kr"

        soup = BeautifulSoup(resp.text, "html.parser")
        parsed = parse_kacta_table(soup)

        for item in parsed:
            rows.append({
                "지역":    region,
                "상세지역": detail,
                "이름":    item["이름"],
                "전화번호": item["전화번호"],
                "현황":    item["현황"],
            })

    except requests.exceptions.Timeout:
        st.warning(f"⏱️ 타임아웃: {region} {detail}")
    except Exception as e:
        st.warning(f"⚠️ {region} {detail} — {type(e).__name__}: {e}")

    return rows


# 결과 컬럼 순서 (샘플 파일과 동일)
RESULT_COLS = ["지역", "상세지역", "이름", "전화번호", "현황"]


def crawl_all(url_df: pd.DataFrame, prog, status_text) -> pd.DataFrame:
    session = requests.Session()
    try:
        session.get("https://www.kacta.or.kr/", timeout=10)
    except Exception:
        pass

    all_data: list[dict] = []
    total = len(url_df)

    for idx, (_, row) in enumerate(url_df.iterrows()):
        status_text.text(f"🔄 {row['지역']} {row['상세지역']} ({idx+1}/{total})")
        prog.progress((idx + 1) / total)
        all_data.extend(crawl_one(row["지역"], row["상세지역"], row["URL"], session))
        time.sleep(0.4)  # 서버 부하 방지

    return (
        pd.DataFrame(all_data, columns=RESULT_COLS)
        if all_data
        else pd.DataFrame(columns=RESULT_COLS)
    )


# ── Google Sheets 업로드 ──────────────────────────────────────────────────────
def upload_to_sheet(spreadsheet, df: pd.DataFrame, sheet_name: str):
    """날짜 이름으로 새 시트 생성 후 데이터 업로드"""
    try:
        ws = spreadsheet.add_worksheet(
            title=sheet_name,
            rows=max(len(df) + 5, 100),
            cols=len(df.columns) + 1,
        )
    except gspread.exceptions.APIError:
        # 이미 존재하면 초기화
        ws = spreadsheet.worksheet(sheet_name)
        ws.clear()

    data = [df.columns.tolist()] + df.fillna("").astype(str).values.tolist()
    ws.update(data, value_input_option="RAW")

    # 헤더 스타일 (네이비 배경 + 흰 텍스트 + 볼드 + 가운데)
    end_col = chr(64 + len(df.columns))
    ws.format(f"A1:{end_col}1", {
        "textFormat": {
            "bold": True,
            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
        },
        "backgroundColor": {"red": 0.12, "green": 0.23, "blue": 0.54},
        "horizontalAlignment": "CENTER",
    })
    return ws


# ── 이전 조회 시트 로드 ───────────────────────────────────────────────────────
def load_date_sheets(spreadsheet) -> list[str]:
    return sorted(
        [ws.title for ws in spreadsheet.worksheets()
         if re.match(r"\d{4}-\d{2}-\d{2}", ws.title)]
    )


def load_previous_data(spreadsheet) -> tuple[pd.DataFrame | None, str | None]:
    sheets = load_date_sheets(spreadsheet)
    if len(sheets) < 2:
        return None, (sheets[-1] if sheets else None)
    prev_title = sheets[-2]
    records = spreadsheet.worksheet(prev_title).get_all_records()
    return pd.DataFrame(records), prev_title


# ── 신규 비교 ─────────────────────────────────────────────────────────────────
KEY_COLS = ["지역", "상세지역", "이름", "전화번호"]


def find_new_entries(current_df: pd.DataFrame, previous_df: pd.DataFrame | None) -> pd.DataFrame:
    if previous_df is None or previous_df.empty:
        return current_df.copy()

    prev_keys = set(
        previous_df.reindex(columns=KEY_COLS).fillna("").apply(
            lambda r: tuple(r.astype(str)), axis=1
        )
    )
    mask = current_df.reindex(columns=KEY_COLS).fillna("").apply(
        lambda r: tuple(r.astype(str)) not in prev_keys, axis=1
    )
    return current_df[mask].copy()


# ── 다운로드 헬퍼 ─────────────────────────────────────────────────────────────
def make_excel(dfs: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for sheet_name, df in dfs.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            ws = writer.sheets[sheet_name]
            # 한글 호환 폰트 지정
            from openpyxl.styles import Font, PatternFill, Alignment
            header_font  = Font(name="맑은 고딕", bold=True, color="FFFFFF")
            header_fill  = PatternFill("solid", fgColor="1E3A8A")
            header_align = Alignment(horizontal="center", vertical="center")
            cell_font    = Font(name="맑은 고딕", size=10)
            for cell in ws[1]:
                cell.font      = header_font
                cell.fill      = header_fill
                cell.alignment = header_align
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    cell.font = cell_font
            # 컬럼 너비 자동 조정
            for col in ws.columns:
                max_len = max((len(str(c.value)) if c.value else 0) for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max_len * 2 + 2, 40)
    return buf.getvalue()


def make_region_summary(df: pd.DataFrame) -> pd.DataFrame:
    """지역 / 상세지역별 건수 집계"""
    summary = (
        df.groupby(["지역", "상세지역"], sort=False)
        .size()
        .reset_index(name="건수")
    )
    total = pd.DataFrame([{"지역": "합계", "상세지역": "", "건수": len(df)}])
    return pd.concat([summary, total], ignore_index=True)


# ═══════════════════════════════════════════════════════════════════════════════
# UI
# ═══════════════════════════════════════════════════════════════════════════════
st.title("🔍 개업 세무사 조회")
st.caption("kacta.or.kr 지역별 세무사 정보 수집 → Google Sheets 날짜별 저장 + 신규 비교")

# 사이드바
with st.sidebar:
    sheet_id = st.text_input(
        "Google Sheets ID",
        value="18ld7dK3aAmJljJRNTtF3_oWi-M007hr9F_FHfYgl0vc",
        help="스프레드시트 URL의 /d/XXXX/ 부분"
    )

# 메인 버튼
col_a, col_b = st.columns([3, 1])
with col_a:
    run_btn = st.button("🚀 조회 시작", type="primary", use_container_width=True)
with col_b:
    preview_btn = st.button("👁️ URL 목록", use_container_width=True)

# ── URL 미리보기 ──────────────────────────────────────────────────────────────
if preview_btn:
    with st.spinner("Google Sheets 연결 중..."):
        try:
            client = get_gspread_client()
            sp = get_spreadsheet(client, sheet_id)
            url_df = load_url_list(sp)
            st.success(f"총 **{len(url_df)}**개 지역 URL 확인")
            c1, c2 = st.columns([1, 2])
            summary = url_df.groupby("지역")["상세지역"].count().reset_index()
            summary.columns = ["지역", "지역구 수"]
            c1.dataframe(summary, use_container_width=True)
            c2.dataframe(url_df, use_container_width=True, height=400)
        except Exception as e:
            st.error(f"오류: {e}")

# ── 조회 실행 ───────────────────────────────────────────────────────────────
if run_btn:
    try:
        client = get_gspread_client()
        sp = get_spreadsheet(client, sheet_id)

        with st.spinner("URL 목록 로드 중..."):
            url_df = load_url_list(sp)

        if url_df.empty:
            st.error("URL 목록이 비어 있습니다.")
            st.stop()

        st.info(f"총 **{len(url_df)}**개 지역 조회 시작합니다.")

        st.subheader("📡 조회 진행")
        prog = st.progress(0)
        status_text = st.empty()
        today = datetime.now().strftime("%Y-%m-%d")

        current_df = crawl_all(url_df, prog, status_text)
        status_text.text(f"✅ 완료 — {len(current_df):,}건 수집")

        if current_df.empty:
            st.error("수집된 데이터가 없습니다. 사이트 접근 차단 또는 구조 변경을 확인해주세요.")
            st.stop()

        # Google Sheets 저장
        with st.spinner(f"'{today}' 시트 저장 중..."):
            upload_to_sheet(sp, current_df, today)
        st.success(f"✅ Google Sheets `{today}` 시트 저장 완료 ({len(current_df):,}건)")

        # 이전 데이터 비교
        with st.spinner("이전 조회와 비교 중..."):
            previous_df, prev_title = load_previous_data(sp)
        new_df = find_new_entries(current_df, previous_df)

        # ── 요약 지표 ─────────────────────────────────────────────────────
        st.markdown("---")
        st.subheader("📊 수집 결과")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("총 수집 건수", f"{len(current_df):,}건")
        m2.metric(
            "🆕 신규 등록",
            f"{len(new_df):,}건",
            delta=f"+{len(new_df)}" if len(new_df) > 0 else None,
        )
        m3.metric("비교 기준 시트", prev_title or "없음 (첫 조회)")
        m4.metric("수집 지역 수", f"{current_df['지역'].nunique()}개")

        # 지역 / 상세지역별 건수
        st.markdown("**📍 지역 · 상세지역별 건수**")
        region_summary = make_region_summary(current_df)
        st.dataframe(
            region_summary,
            use_container_width=True,
            hide_index=True,
            height=min(36 * len(region_summary) + 38, 420),
            column_config={
                "지역":   st.column_config.TextColumn("지역",   width="small"),
                "상세지역": st.column_config.TextColumn("상세지역", width="small"),
                "건수":   st.column_config.NumberColumn("건수",  format="%d건"),
            },
        )

        # 현황별 집계
        status_counts = current_df["현황"].value_counts().reset_index()
        status_counts.columns = ["현황", "건수"]
        with st.expander("현황별 집계 보기"):
            st.dataframe(status_counts, use_container_width=True)

        # ── 신규 데이터 지역별 출력 ────────────────────────────────────────
        st.markdown("---")
        if not new_df.empty:
            st.subheader(f"🆕 신규 등록 세무사 — {len(new_df):,}건")
            st.caption(f"비교 기준: `{prev_title}` → `{today}`")

            regions_new = new_df["지역"].unique().tolist()

            if len(regions_new) <= 15:
                tabs = st.tabs(regions_new)
                for tab, region in zip(tabs, regions_new):
                    with tab:
                        rdf = new_df[new_df["지역"] == region]
                        details = rdf["상세지역"].unique().tolist()

                        for detail in details:
                            ddf = (
                                rdf[rdf["상세지역"] == detail]
                                [["이름", "전화번호", "현황"]]   # 지역/상세지역 이미 탭 제목
                                .reset_index(drop=True)
                            )
                            st.markdown(f"**📍 {detail}** — {len(ddf)}건")
                            st.dataframe(
                                ddf,
                                use_container_width=True,
                                hide_index=True,
                                column_config={
                                    "이름":    st.column_config.TextColumn("이름",    width="small"),
                                    "전화번호": st.column_config.TextColumn("전화번호", width="medium"),
                                    "현황":    st.column_config.TextColumn("현황",    width="small"),
                                }
                            )
            else:
                # 지역이 너무 많으면 전체 테이블
                st.dataframe(
                    new_df[RESULT_COLS].reset_index(drop=True),
                    use_container_width=True,
                    height=520,
                    hide_index=True,
                )

            # ── 다운로드 ──────────────────────────────────────────────────
            st.markdown("---")
            st.subheader("⬇️ 다운로드")

            dc1, dc2, dc3 = st.columns(3)

            dc1.download_button(
                label="📥 신규 데이터 CSV",
                data=new_df[RESULT_COLS].to_csv(index=False, encoding="utf-8-sig"),
                file_name=f"신규세무사_{today}.csv",
                mime="text/csv",
                use_container_width=True,
            )

            dc2.download_button(
                label="📥 전체 데이터 CSV",
                data=current_df[RESULT_COLS].to_csv(index=False, encoding="utf-8-sig"),
                file_name=f"전체세무사_{today}.csv",
                mime="text/csv",
                use_container_width=True,
            )

            dc3.download_button(
                label="📥 Excel (신규 + 전체)",
                data=make_excel({
                    "신규등록":  new_df[RESULT_COLS],
                    "전체데이터": current_df[RESULT_COLS],
                }),
                file_name=f"세무사_{today}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        else:
            # 신규 없음
            if previous_df is not None:
                st.success("✅ 이전 조회 대비 신규 등록 데이터가 없습니다.")
            else:
                st.info("📌 첫 번째 조회입니다. 다음 조회부터 신규 비교가 가능합니다.")

            # 전체 데이터 다운로드만 제공
            c1, c2 = st.columns(2)
            c1.download_button(
                "📥 전체 데이터 CSV",
                data=current_df[RESULT_COLS].to_csv(index=False, encoding="utf-8-sig"),
                file_name=f"전체세무사_{today}.csv",
                mime="text/csv",
                use_container_width=True,
            )
            c2.download_button(
                "📥 전체 데이터 Excel",
                data=make_excel({"전체데이터": current_df[RESULT_COLS]}),
                file_name=f"전체세무사_{today}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

    except Exception as e:
        st.error(f"❌ 오류 발생: {e}")
        st.exception(e)


# ── 이전 조회 기록 열람 + 삭제 ───────────────────────────────────────────────
st.markdown("---")
st.subheader("📅 이전 조회 기록")

try:
    client = get_gspread_client()
    sp = get_spreadsheet(client, sheet_id)
    date_sheets = load_date_sheets(sp)[::-1]  # 최신 순

    if date_sheets:
        tab_view, tab_delete = st.tabs(["📂 불러오기", "🗑️ 시트 삭제"])

        # ── 불러오기 탭 ──────────────────────────────────────────────────────
        with tab_view:
            selected = st.selectbox("날짜 선택", date_sheets, key="view_select")
            if st.button("📂 불러오기", key="load_btn"):
                records = sp.worksheet(selected).get_all_records()
                hist_df = pd.DataFrame(records)
                ordered_cols = [c for c in RESULT_COLS if c in hist_df.columns]
                hist_df = hist_df[ordered_cols]

                st.caption(f"총 {len(hist_df):,}건")

                # 지역 / 상세지역별 건수
                with st.expander("📍 지역 · 상세지역별 건수"):
                    st.dataframe(
                        make_region_summary(hist_df),
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "건수": st.column_config.NumberColumn("건수", format="%d건"),
                        },
                    )

                st.dataframe(hist_df, use_container_width=True, height=420, hide_index=True)

                hc1, hc2 = st.columns(2)
                hc1.download_button(
                    f"📥 {selected} CSV",
                    data=hist_df.to_csv(index=False, encoding="utf-8-sig"),
                    file_name=f"세무사_{selected}.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
                hc2.download_button(
                    f"📥 {selected} Excel",
                    data=make_excel({selected: hist_df}),
                    file_name=f"세무사_{selected}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )

        # ── 삭제 탭 ──────────────────────────────────────────────────────────
        with tab_delete:
            st.caption("삭제할 시트를 선택하세요. 삭제 후 복구가 불가합니다.")

            to_delete = st.multiselect(
                "삭제할 날짜 시트 선택",
                options=date_sheets,
                placeholder="시트를 선택하세요...",
                key="delete_select",
            )

            if to_delete:
                st.warning(
                    f"**{len(to_delete)}개** 시트가 삭제됩니다: "
                    + ", ".join(f"`{s}`" for s in to_delete)
                )

                # 확인 체크박스 → 버튼 활성화
                confirmed = st.checkbox(
                    "위 시트를 영구 삭제하겠습니다. (복구 불가)",
                    key="delete_confirm",
                )

                del_btn = st.button(
                    f"🗑️ {len(to_delete)}개 시트 삭제",
                    type="primary",
                    disabled=not confirmed,
                    key="delete_btn",
                )

                if del_btn and confirmed:
                    success, failed = [], []
                    prog_del = st.progress(0)
                    for i, sheet_name in enumerate(to_delete):
                        try:
                            ws = sp.worksheet(sheet_name)
                            sp.del_worksheet(ws)
                            success.append(sheet_name)
                        except Exception as e:
                            failed.append(f"{sheet_name} ({e})")
                        prog_del.progress((i + 1) / len(to_delete))

                    if success:
                        st.success(
                            f"✅ 삭제 완료: "
                            + ", ".join(f"`{s}`" for s in success)
                        )
                    if failed:
                        st.error("❌ 삭제 실패: " + ", ".join(failed))

                    # 캐시 초기화 후 목록 갱신
                    st.cache_resource.clear()
                    st.rerun()
            else:
                st.info("삭제할 시트를 위에서 선택해주세요.")

    else:
        st.caption("아직 조회 기록이 없습니다. 크롤링을 먼저 실행해주세요.")

except Exception as e:
    st.caption(f"기록 로드 실패: {e}")
