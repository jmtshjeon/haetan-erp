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
            "업로드 파일 단가": float(price) if isfinite(price) else None,
        }
        if len(group) > 1:
            reason = "엑셀에 같은 상품코드가 여러 줄 있음"
        elif not existing:
            reason = OUTSIDE_COST_SCOPE
        elif not isfinite(price) or price <= 0 or existing[1] is None:
            reason = "기존 단가 또는 업로드 파일 단가 확인 필요"
        if reason:
            review.append({**entry, "확인 사항": reason})
        elif price != existing[1]:
            changes.append({**entry, "변동액": float(price) - existing[1]})
    return changes, review


def apply_prices(conn, chosen, record_price_change, mark_cost_update):
    with conn:
        cursor = conn.cursor()
        for row in chosen:
            code, old, new = row["앱 상품코드"], row["기존 단가"], row["업로드 파일 단가"]
            cursor.execute(
                "UPDATE ingredients SET supply_price=? WHERE item_code=? AND supply_price=?",
                (new, code, old),
            )
            if cursor.rowcount != 1:
                raise ValueError("다른 단가가 먼저 저장되었습니다. 파일을 다시 올려 비교해 주세요.")
            record_price_change(cursor, code, row["앱 재료명"], old, new)
        if chosen:
            mark_cost_update(cursor)


def apply_reviewed_price(conn, original, price, capacity, record_price_change, mark_cost_update):
    name, old_price, old_capacity, unit, code = original
    if not isfinite(price) or price <= 0 or not isfinite(capacity) or capacity <= 0:
        raise ValueError("단가와 포장용량은 0보다 커야 합니다.")
    if (price, capacity) == (old_price, old_capacity):
        raise ValueError("단가 또는 포장용량을 변경해 주세요.")
    with conn:
        cursor = conn.execute(
            """UPDATE ingredients SET supply_price=?, total_capacity=?
               WHERE item_code=? AND item_name IS ? AND supply_price IS ?
               AND total_capacity IS ? AND unit IS ?""",
            (price, capacity, code, name, old_price, old_capacity, unit),
        )
        if cursor.rowcount != 1:
            raise ValueError("품목 정보가 먼저 변경되었습니다. 다시 비교한 후 저장해 주세요.")
        record_price_change(cursor, code, name, old_price, price)
        mark_cost_update(cursor)


def review_key(row, master):
    return (row["상품코드"], row["앱 상품코드"], row["조회 상품명"], row["조회 규격"],
            row["업로드 파일 단가"], master.get(row["앱 상품코드"]))


def show_review_editor(st, rows, master, completed, get_connection, record_price_change, mark_cost_update):
    linked = {row["상품코드"]: row for row in rows if row["앱 상품코드"] in master}
    if not linked:
        return
    source_code = st.selectbox(
        "직접 수정할 품목", list(linked), index=None, key="price_review_item",
        format_func=lambda code: f"{linked[code]['앱 재료명']} · {code}",
        placeholder="품목을 선택해 주세요",
    )
    if source_code is None:
        return
    row = linked[source_code]
    code = row["앱 상품코드"]
    name, old_price, old_capacity, unit = master[code]
    original = (name, old_price, old_capacity, unit, code)
    st.write(f"**확인 사항** · {row['확인 사항']}")
    quoted = row["업로드 파일 단가"]
    quoted_text = f"{int(quoted):,}원" if quoted is not None else "없음"
    st.caption(f"조회 상품 {source_code} · {row['조회 상품명']} · {row['조회 규격'] or '규격 없음'} · {quoted_text}")
    st.caption(f"저장 대상 {code} · {name} · 기존 포장 {row['앱 포장 기준'] or '미등록'}")
    if st.button("기존 값 유지 · 확인 완료", key="price_review_keep"):
        completed[review_key(row, master)] = "기존 값 유지"
        st.session_state["price_review_kept"] = name
        st.rerun()
    signature = sha256(repr((source_code, original)).encode("utf-8")).hexdigest()
    with st.form(f"price_review:{signature}"):
        price_column, capacity_column = st.columns(2)
        price = price_column.number_input("적용 단가 (원)", min_value=0.0,
                                          value=float(old_price or 0), step=1.0, format="%d")
        capacity = capacity_column.number_input(f"적용 포장용량 ({unit or '기존 단위'})", min_value=0.0,
                                                value=float(old_capacity or 0), step=1.0)
        st.caption("기존 값에서 시작합니다. 적용할 가격과 포장용량을 입력해 주세요.")
        if st.form_submit_button("확인한 품목 저장", type="primary"):
            try:
                with closing(get_connection()) as conn:
                    apply_reviewed_price(conn, original, price, capacity, record_price_change, mark_cost_update)
            except ValueError as error:
                st.error(str(error))
            else:
                st.cache_data.clear()
                updated_master = {code: (name, price, capacity, unit)}
                completed[review_key(row, updated_master)] = "수정 저장"
                st.session_state["price_review_saved"] = name
                st.rerun()


def show_price_update(st, get_connection, record_price_change, mark_cost_update):
    kept_name = st.session_state.pop("price_review_kept", None)
    if kept_name:
        st.toast("기존 값 유지 · 확인 완료", icon="✅")
        st.success(f"확인 완료 · {kept_name}의 기존 단가와 포장용량을 유지했습니다.")
    reviewed_name = st.session_state.pop("price_review_saved", None)
    if reviewed_name:
        st.toast("확인한 품목 저장 완료", icon="✅")
        st.success(f"저장 완료 · {reviewed_name}의 단가와 포장용량을 저장했습니다.")
    applied_count = st.session_state.pop("price_update_applied_count", None)
    if applied_count is not None:
        st.toast(f"단가 {applied_count}건 저장 완료", icon="✅")
        st.success(f"저장 완료 · 단가 {applied_count}건을 저장했습니다.")
        st.caption("저장한 단가는 가맹점 화면의 식재료단가에서 확인할 수 있습니다.")
        st.button(
            "가맹점 화면에서 저장 내역 확인",
            on_click=st.session_state.update,
            args=({"view_mode": "가맹점 화면", "franchise_menu": "식재료단가 · 푸디스트 기준"},),
        )
    st.write("푸디스트 즐겨찾기 파일을 올리고, 앱 포장 기준과 조회 규격을 비교한 뒤 단가를 저장하세요.")
    upload = st.file_uploader("푸디스트 즐겨찾기 파일", type=["xlsx", "csv"])
    if upload is None:
        return
    upload_signature = sha256(upload.getbuffer()).hexdigest()
    previous_signature, completed = st.session_state.get("price_review_completed", ("", {}))
    if upload_signature != previous_signature:
        completed = {}
    st.session_state["price_review_completed"] = upload_signature, completed
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

    price_columns = {
        name: st.column_config.NumberColumn(name, format="%d 원")
        for name in ("기존 단가", "업로드 파일 단가", "변동액")
    }
    if changes:
        st.markdown("#### 바뀐 단가")
        signature = sha256(repr(changes).encode("utf-8")).hexdigest()
        state_key = "price_update_bulk_selection"
        saved_signature, revision, select_all = st.session_state.get(state_key, ("", 0, False))
        if saved_signature != signature:
            revision, select_all = 0, False
        checked = st.checkbox("저장 대상 · 전체 선택", value=select_all, key=f"price_update_all:{signature}")
        if checked != select_all:
            revision, select_all = revision + 1, checked
        st.session_state[state_key] = signature, revision, select_all
        table = pd.DataFrame(changes)
        table.insert(0, "저장 대상", select_all)
        edited = st.data_editor(
            table, disabled=[name for name in table if name != "저장 대상"],
            hide_index=True, use_container_width=True, column_config=price_columns,
            key=f"price_update_editor:{signature}:{revision}",
        )
        selected_codes = set(edited.loc[edited["저장 대상"], "상품코드"])
        chosen = [row for row in changes if row["상품코드"] in selected_codes]
        if st.button(f"선택한 단가 {len(chosen)}건 저장", disabled=not chosen, type="primary"):
            try:
                with closing(get_connection()) as conn:
                    apply_prices(conn, chosen, record_price_change, mark_cost_update)
            except ValueError as error:
                st.error(str(error))
            else:
                st.cache_data.clear()
                st.session_state["price_update_applied_count"] = len(chosen)
                st.rerun()
    else:
        st.info("저장할 단가 변경이 없습니다.")
    needs_review = [row for row in review if row["확인 사항"] != OUTSIDE_COST_SCOPE
                    and review_key(row, master) not in completed]
    skipped = [row for row in review if row["확인 사항"] == OUTSIDE_COST_SCOPE]
    if needs_review:
        with st.expander(f"확인 필요 · {len(needs_review)}개", expanded=True):
            st.caption(
                "앱 재료와 조회 상품의 규격을 비교하세요. 원가에 쓸 기준을 확인한 뒤 아래에서 한 품목씩 저장할 수 있습니다."
            )
            st.dataframe(pd.DataFrame(needs_review), hide_index=True, use_container_width=True,
                         column_config=price_columns)
            show_review_editor(st, needs_review, master, completed, get_connection,
                               record_price_change, mark_cost_update)
    completed_rows = [
        {"상품코드": source, "앱 재료명": data[0], "처리 결과": status}
        for (source, code, _, _, _, data), status in completed.items()
        if data is not None and data == master.get(code)
    ]
    if completed_rows:
        with st.expander(f"확인 완료 · {len(completed_rows)}개"):
            st.dataframe(pd.DataFrame(completed_rows).drop_duplicates(), hide_index=True)
    if skipped:
        with st.expander(f"원가 연동 대상 아님 · {len(skipped)}개 건너뜀"):
            st.caption("현재 메뉴 원가에 쓰이는 식재료 코드와 연결되지 않은 품목입니다. 실제 사용 품목이 여기 있으면 앱의 등록 코드와 조회 코드를 확인하세요.")
            st.dataframe(
                pd.DataFrame(skipped)[["상품코드", "조회 상품명", "조회 규격", "업로드 파일 단가"]],
                hide_index=True, use_container_width=True, column_config=price_columns,
            )
