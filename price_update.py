from contextlib import closing
from math import isfinite

import pandas as pd


REVIEW_CODES = {
    "F231840": "계란 30알과 앱의 중량 기준 확인",
    "F322126": "건마카로니와 완성 샐러드의 가격 기준이 다름",
    "F347406": "식용유 한 통과 부재료 1회분의 가격 기준이 다름",
    "F119932": "월계수잎 브랜드 확인",
    "F331400": "식용유 상품명과 규격 확인",
}
OUTSIDE_COST_SCOPE = "메뉴 원가에 연결된 기존 품목이 아님"


def compare_prices(frame, master):
    frame = frame.copy()
    frame.columns = frame.columns.str.strip()
    if not {"상품코드", "상품명", "단가"} <= set(frame.columns):
        raise ValueError("상품코드, 상품명, 단가가 있는 푸디스트 즐겨찾기 파일을 사용해 주세요.")
    if "발주상태" in frame or "발주시간" in frame:
        raise ValueError("푸디스트 원가관리용 즐겨찾기에서 내려받은 파일을 사용해 주세요.")
    frame["상품코드"] = frame["상품코드"].fillna("").astype(str).str.strip()
    frame = frame.loc[frame["상품코드"].str.fullmatch(r"F[0-9]+")]
    if frame.empty:
        raise ValueError("파일에 푸디스트 상품코드가 없습니다.")

    changes, review = [], []
    for code, group in frame.groupby("상품코드", sort=False):
        row = group.iloc[0]
        price = pd.to_numeric(row["단가"], errors="coerce")
        existing = master.get(code)
        spec = row.get("규격", "")
        entry = {
            "상품코드": code, "앱 재료명": existing[0] if existing else "",
            "조회 상품명": str(row["상품명"]).strip(), "조회 규격": "" if pd.isna(spec) else str(spec),
            "기존 단가": existing[1] if existing else None,
            "조회 단가": float(price) if isfinite(price) else None,
        }
        reason = REVIEW_CODES.get(code)
        if len(group) > 1:
            reason = "엑셀에 같은 상품코드가 여러 줄 있음"
        elif not existing:
            reason = OUTSIDE_COST_SCOPE
        elif not isfinite(price) or price <= 0 or existing[1] is None:
            reason = "기존 단가 또는 조회 단가 확인 필요"
        if reason:
            review.append({**entry, "확인 사항": reason})
        elif price != existing[1]:
            changes.append({**entry, "변동액": float(price) - existing[1]})
    return changes, review


def apply_prices(conn, chosen, record_price_change):
    with conn:
        cursor = conn.cursor()
        for row in chosen:
            code, old, new = row["상품코드"], row["기존 단가"], row["조회 단가"]
            cursor.execute(
                "UPDATE ingredients SET supply_price=? WHERE item_code=? AND supply_price=?",
                (new, code, old),
            )
            if cursor.rowcount != 1:
                raise ValueError("다른 단가가 먼저 저장되었습니다. 파일을 다시 올려 비교해 주세요.")
            record_price_change(cursor, code, row["앱 재료명"], old, new)


def show_price_update(st, get_connection, record_price_change):
    st.write("푸디스트 원가관리용 즐겨찾기 엑셀을 올리고, 반영할 상품을 선택하세요.")
    upload = st.file_uploader("푸디스트 즐겨찾기 파일", type=["xlsx", "csv"])
    if upload is None:
        return
    try:
        frame = pd.read_csv(upload) if upload.name.lower().endswith(".csv") else pd.read_excel(upload)
        with closing(get_connection()) as conn:
            master = {
                code: (name, price)
                for code, name, price in conn.execute("""
                    WITH RECURSIVE used(name) AS (
                        SELECT TRIM(ingredient_name) FROM menu_bom WHERE menu_type <> '프랩'
                        UNION
                        SELECT TRIM(bom.ingredient_name) FROM menu_bom AS bom
                        JOIN used ON TRIM(bom.menu_name) = used.name
                    )
                    SELECT item_code, item_name, supply_price FROM ingredients
                    JOIN used ON TRIM(ingredients.item_name) = used.name
                    WHERE NOT EXISTS (
                        SELECT 1 FROM menu_bom WHERE TRIM(menu_name) = used.name
                    )
                """)
            }
        changes, review = compare_prices(frame, master)
    except (ValueError, OSError) as error:
        st.error(str(error))
        return

    if changes:
        st.markdown("#### 바뀐 단가")
        table = pd.DataFrame(changes)
        table.insert(0, "반영", False)
        edited = st.data_editor(
            table, disabled=[name for name in table if name != "반영"],
            hide_index=True, use_container_width=True,
        )
        selected_codes = set(edited.loc[edited["반영"], "상품코드"])
        chosen = [row for row in changes if row["상품코드"] in selected_codes]
        if st.button(f"선택한 {len(chosen)}개 단가 반영", disabled=not chosen):
            try:
                with closing(get_connection()) as conn:
                    apply_prices(conn, chosen, record_price_change)
            except ValueError as error:
                st.error(str(error))
            else:
                st.cache_data.clear()
                st.success(f"{len(chosen)}개 단가를 반영하고 변동 이력에 기록했습니다.")
    else:
        st.info("반영할 단가 변경이 없습니다.")
    needs_review = [row for row in review if row["확인 사항"] != OUTSIDE_COST_SCOPE]
    skipped = [row for row in review if row["확인 사항"] == OUTSIDE_COST_SCOPE]
    if needs_review:
        with st.expander(f"확인 필요 · {len(needs_review)}개", expanded=True):
            st.caption("아래 품목은 이번 단가 갱신에 포함하지 않습니다.")
            st.dataframe(pd.DataFrame(needs_review), hide_index=True, use_container_width=True)
    if skipped:
        with st.expander(f"원가 연동 대상 아님 · {len(skipped)}개 건너뜀"):
            st.dataframe(
                pd.DataFrame(skipped)[["상품코드", "조회 상품명", "조회 규격", "조회 단가"]],
                hide_index=True, use_container_width=True,
            )
