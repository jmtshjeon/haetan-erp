from contextlib import closing
from hashlib import sha256
from math import isfinite
import re

import pandas as pd


REVIEW_CODES = {
    "F231840": "계란 30알과 앱의 중량 기준 확인",
    "F322126": "건마카로니와 완성 샐러드의 가격 기준이 다름",
    "F347406": "식용유 한 통과 부재료 1회분의 가격 기준이 다름",
    "F119932": "월계수잎 브랜드 확인",
    "F331400": "식용유 상품명과 규격 확인",
}
OUTSIDE_COST_SCOPE = "메뉴 원가에 연결된 기존 품목이 아님"
REPLACEMENT_CODES = {
    "F369915": ("F339571", "연포탕육수", "2kg*5ea/box"),
    "F369916": ("F339572", "볶음소스", "2kg*5ea/box"),
    "F369917": ("F339573", "무침소스", "2kg*5ea/box"),
    "F369918": ("F339574", "수육육수", "2kg*5ea/box"),
    "F369919": ("F343382", "유자폰즈소스", "2kg*5ea/box"),
    "F370155": ("F339570", "오징어튀김가루", "1kg*10ea/box"),
}


def weight_in_grams(spec, order_unit):
    if str(order_unit).strip().upper() == "KG":
        return 1000
    text = str(spec).lower().replace(",", "")
    number = r"(\d+(?:\.\d+)?)\s*(kg|g)\b"
    pack = re.search(number + r"\s*[x×*]\s*(\d+)\s*(?:ea|개입|개|팩)\s*/?\s*box", text)
    if pack:
        amount, unit, count = pack.groups()
        return float(amount) * (1000 if unit == "kg" else 1) * int(count)
    simple = re.match(r"\s*(?:\[box\]\s*|ea\s*/\s*)?" + number, text)
    if simple:
        amount, unit = simple.groups()
        if str(order_unit).strip().upper() == "BOX" and re.search(r"[x×*]", text[simple.end():]):
            return None
        return float(amount) * (1000 if unit == "kg" else 1)
    return None


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
    source_codes = set(frame["상품코드"])
    for code, group in frame.groupby("상품코드", sort=False):
        row = group.iloc[0]
        price = pd.to_numeric(row["단가"], errors="coerce")
        target = code
        spec = row.get("규격", "")
        product = str(row["상품명"]).strip()
        reason = REVIEW_CODES.get(code)
        if code not in master and code in REPLACEMENT_CODES:
            previous, expected_name, expected_spec = REPLACEMENT_CODES[code]
            if previous in master:
                target = previous
                if (
                    expected_name not in "".join(product.split())
                    or "".join(str(spec).lower().split()) != expected_spec
                    or master[previous][2:] != (10000, "g")
                ):
                    reason = "신규 코드의 상품명이나 10kg 박스 규격 확인 필요"
                elif previous in source_codes:
                    reason = "기존 코드와 신규 코드가 모두 있어 어느 단가를 쓸지 확인 필요"
        existing = master.get(target)
        capacity, unit = existing[2:] if existing else (None, "")
        new_weight = weight_in_grams(spec, row.get("단위", ""))
        app_pack = f"{capacity:,.0f} {unit}" if capacity is not None else ""
        comparison = "규격 직접 확인"
        if unit == "g" and new_weight is not None and capacity is not None:
            if abs(new_weight - capacity) <= 0.01:
                comparison = "포장 중량 일치"
            else:
                comparison = f"포장 중량 다름 · 조회 {new_weight:,.0f} g"
                reason = reason or "포장 중량 변경 확인 필요"
        entry = {
            "상품코드": code, "앱 상품코드": target, "앱 재료명": existing[0] if existing else "",
            "조회 상품명": product, "앱 포장 기준": app_pack,
            "조회 규격": "" if pd.isna(spec) else str(spec), "규격 비교": comparison,
            "기존 단가": existing[1] if existing else None,
            "조회 단가": float(price) if isfinite(price) else None,
        }
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
            code, old, new = row["앱 상품코드"], row["기존 단가"], row["조회 단가"]
            cursor.execute(
                "UPDATE ingredients SET supply_price=? WHERE item_code=? AND supply_price=?",
                (new, code, old),
            )
            if cursor.rowcount != 1:
                raise ValueError("다른 단가가 먼저 저장되었습니다. 파일을 다시 올려 비교해 주세요.")
            record_price_change(cursor, code, row["앱 재료명"], old, new)


def show_price_update(st, get_connection, record_price_change):
    st.write("푸디스트 즐겨찾기 파일을 올리고, 앱 포장 기준과 조회 규격을 비교한 뒤 단가를 반영하세요.")
    upload = st.file_uploader("푸디스트 즐겨찾기 파일", type=["xlsx", "csv"])
    if upload is None:
        return
    try:
        frame = pd.read_csv(upload) if upload.name.lower().endswith(".csv") else pd.read_excel(upload)
        with closing(get_connection()) as conn:
            master = {
                code: (name, price, capacity, unit)
                for code, name, price, capacity, unit in conn.execute("""
                    WITH RECURSIVE used(name) AS (
                        SELECT TRIM(ingredient_name) FROM menu_bom WHERE menu_type <> '프랩'
                        UNION
                        SELECT TRIM(bom.ingredient_name) FROM menu_bom AS bom
                        JOIN used ON TRIM(bom.menu_name) = used.name
                    )
                    SELECT item_code, item_name, supply_price, total_capacity, unit FROM ingredients
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
        signature = sha256(repr(changes).encode("utf-8")).hexdigest()
        state_key = "price_update_bulk_selection"
        saved_signature, revision, select_all = st.session_state.get(state_key, ("", 0, False))
        if saved_signature != signature:
            revision, select_all = 0, False
        checked = st.checkbox("반영 · 전체 선택", value=select_all, key=f"price_update_all:{signature}")
        if checked != select_all:
            revision, select_all = revision + 1, checked
        st.session_state[state_key] = signature, revision, select_all
        table = pd.DataFrame(changes)
        table.insert(0, "반영", select_all)
        edited = st.data_editor(
            table, disabled=[name for name in table if name != "반영"],
            hide_index=True, use_container_width=True,
            key=f"price_update_editor:{signature}:{revision}",
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
            st.caption(
                "이 목록은 자동 반영되지 않습니다. 같은 상품·포장 기준이면 관리자 전용의 "
                "‘식재료 정보 관리’에서 단가를 직접 수정하세요. 실제 포장량이 바뀌었다면 단가와 포장용량을 함께 수정하세요. "
                "다른 상품이나 완성품·부재료 묶음이면 "
                "기존 단가를 유지하고, 배합 원가가 확인되면 그 값을 직접 수정하세요."
            )
            st.dataframe(pd.DataFrame(needs_review), hide_index=True, use_container_width=True)
    if skipped:
        with st.expander(f"원가 연동 대상 아님 · {len(skipped)}개 건너뜀"):
            st.caption("현재 메뉴 원가에 쓰이는 식재료 코드와 연결되지 않은 품목입니다. 실제 사용 품목이 여기 있으면 앱의 등록 코드와 조회 코드를 확인하세요.")
            st.dataframe(
                pd.DataFrame(skipped)[["상품코드", "조회 상품명", "조회 규격", "조회 단가"]],
                hide_index=True, use_container_width=True,
            )
