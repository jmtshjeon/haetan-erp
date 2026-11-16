import json
from copy import deepcopy
from contextlib import closing
from hashlib import sha256
from html import escape
from pathlib import Path
from tempfile import NamedTemporaryFile


ROOT = Path(__file__).resolve().parent
RECIPE_FOLDER = ROOT / "레시피"


def find_photo(folder, stem):
    for extension in (".jpg", ".jpeg", ".png", ".webp"):
        photo = folder / f"{stem}{extension}"
        if photo.is_file():
            return photo
    return None


def save_recipe(path, recipe, photos, original):
    if path.read_bytes() != original:
        raise ValueError("다른 변경이 저장되었습니다. 편집을 닫고 다시 열어 주세요.")
    for name, data in photos.items():
        (path.parent / name).write_bytes(data)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(recipe, handle, ensure_ascii=False, indent=2)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def photo_note(step, position):
    notes = step.get("photo_notes", [])
    if position <= len(notes):
        return notes[position - 1]
    return {key: step[key] for key in ("description", "caution") if key in step} if position == 1 else {}


def edit_recipe(st, path, recipe, rows, original):
    key = f"recipe-draft:{path}"
    if not st.checkbox("레시피 수정", key=f"{key}:open"):
        st.session_state.pop(key, None)
        return recipe, {}

    with st.form(key):
        updated = deepcopy(recipe)
        photos = {}
        updated["servings"] = st.text_input("기준 분량", recipe["servings"]).strip()
        preparation = st.text_area("조리 전 준비 (한 줄에 한 항목)", "\n".join(recipe.get("preparation", [])))
        updated["preparation"] = [line.strip() for line in preparation.splitlines() if line.strip()]
        with st.expander("재료 표시 이름"):
            labels = updated.setdefault("ingredient_labels", {})
            for name in dict.fromkeys(row[0].strip() for row in rows):
                labels[name] = st.text_input(name, labels.get(name, name), key=f"{key}:label:{name}").strip() or name
        for index, step in enumerate(updated["steps"], start=1):
            with st.expander(f"{index}. {step['title']}", expanded=True):
                notes = []
                for position in range(1, 4):
                    field = f"{key}:{index}:{position}"
                    note = dict(photo_note(step, position))
                    st.markdown(f"**사진 {position}**")
                    note["title"] = st.text_input("제목", note.get("title", ""), key=f"{field}:title")
                    note["description"] = st.text_area("조리 설명", note.get("description", ""), key=f"{field}:body")
                    note["caution"] = st.text_input("주의사항", note.get("caution", ""), key=f"{field}:caution")
                    upload = st.file_uploader("사진 추가 또는 교체", type=["jpg", "jpeg", "png", "webp"], key=f"{field}:photo")
                    if upload:
                        data = upload.getvalue()
                        name = f"{index:02}-{position}-{sha256(data).hexdigest()}{Path(upload.name).suffix.lower()}"
                        photos[name] = data
                        note["photo"] = name
                    notes.append(note)
                step["photo_notes"] = notes
        updated["serving_method"] = st.text_area("제공법", recipe.get("serving_method", ""))
        updated["serving_caution"] = st.text_input("제공 시 주의사항", recipe.get("serving_caution", ""))
        if st.form_submit_button("미리보기"):
            if updated["servings"]:
                base = st.session_state.get(key, (None, None, original))[2]
                st.session_state[key] = updated, photos, base
            else:
                st.session_state.pop(key, None)
                st.error("기준 분량을 입력해 주세요.")

    if key not in st.session_state:
        return recipe, {}
    draft, photos, base = st.session_state[key]
    if st.button("미리본 내용 저장", key=f"{key}:save"):
        try:
            save_recipe(path, draft, photos, base)
        except (OSError, ValueError) as error:
            st.error(f"저장하지 못했습니다. {error}")
        else:
            del st.session_state[key]
            st.success("레시피를 저장했습니다.")
            return draft, {}
    st.info("미리보기 · 아래 내용을 확인한 뒤 저장해 주세요.")
    return draft, photos


def show_recipes(st, get_connection, is_admin=False):
    st.markdown(
        "<style>.st-key-recipe-page{max-width:980px;margin-inline:auto}"
        ".st-key-recipe-page .recipe-table{width:100%;max-width:520px;table-layout:fixed;border-collapse:collapse;font-size:.9rem}"
        ".st-key-recipe-page .recipe-table td{padding:5px 8px;border-bottom:1px solid rgba(128,128,128,.23);overflow-wrap:anywhere}"
        ".st-key-recipe-page .recipe-table thead th{padding:6px 8px;background:rgba(128,128,128,.14);text-align:left;font-weight:700}"
        ".st-key-recipe-page .recipe-table thead th:last-child,.st-key-recipe-page .recipe-table td:last-child"
        "{width:6rem;text-align:right;white-space:nowrap;border-left:1px solid rgba(128,128,128,.23)}"
        ".st-key-recipe-page .recipe-photo-slot{min-height:76px;display:grid;place-items:center;"
        "border:1px dashed rgba(128,128,128,.55);border-radius:6px;font-size:.85rem}</style>",
        unsafe_allow_html=True,
    )
    with st.container(key="recipe-page"):
        render_recipe_page(st, get_connection, is_admin)


def render_recipe_page(st, get_connection, is_admin):
    st.subheader("레시피")
    categories = sorted({file.parent.parent.name for file in RECIPE_FOLDER.glob("*/*/레시피.json")})
    if not categories:
        st.info("등록된 레시피가 없습니다.")
        return

    category = st.radio("분류 폴더", categories, horizontal=True)
    files = sorted((RECIPE_FOLDER / category).glob("*/레시피.json"))
    menu = st.radio("메뉴 선택", [file.parent.name for file in files], horizontal=True)
    recipe_file = RECIPE_FOLDER / category / menu / "레시피.json"
    try:
        original = recipe_file.read_bytes()
        recipe = json.loads(original.decode("utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        st.error("레시피 파일을 읽을 수 없습니다.")
        return

    with closing(get_connection()) as conn:
        rows = conn.execute(
            """SELECT bom.ingredient_name, bom.input_qty,
                      (SELECT unit FROM ingredients
                       WHERE TRIM(item_name) = TRIM(bom.ingredient_name) LIMIT 1)
               FROM menu_bom AS bom
               WHERE TRIM(menu_name) = ? AND menu_type = ?
               ORDER BY bom.id""",
            (menu, category),
        ).fetchall()

    photos = {}
    if is_admin:
        recipe, photos = edit_recipe(st, recipe_file, recipe, rows, original)
    st.markdown(
        f'<h3 style="margin:0;padding:0">{escape(menu)}</h3>'
        f'<p style="margin:2px 0 8px">{escape(recipe["servings"])}</p>',
        unsafe_allow_html=True,
    )
    cover_column, ingredients = st.columns([1, 2])
    with cover_column:
        cover = find_photo(ROOT / "메뉴이미지", menu)
        if cover:
            st.image(str(cover), width=190)
        else:
            st.info("자료 사진 없음")
    with ingredients:
        if rows:
            labels = recipe.get("ingredient_labels", {})
            toppings = set(recipe.get("toppings", []))
            table_rows = []
            for heading, group in (
                ("", [row for row in rows if row[0].strip() not in toppings]),
                ("제공 전 토핑", [row for row in rows if row[0].strip() in toppings]),
            ):
                if not group:
                    continue
                if heading:
                    table_rows.append(f'<tr><th colspan="2" style="text-align:left;padding:9px 8px 3px">{heading}</th></tr>')
                for name, quantity, unit in group:
                    label = escape(labels.get(name.strip(), name))
                    amount = escape(f"{quantity:g} {unit or ''}".strip())
                    table_rows.append(f"<tr><td>{label}</td><td>{amount}</td></tr>")
            st.markdown(
                '<table class="recipe-table"><thead><tr>'
                '<th scope="col">재료</th><th scope="col">투입량</th></tr></thead><tbody>'
                + "".join(table_rows) + "</tbody></table>",
                unsafe_allow_html=True,
            )
        else:
            st.info("원가 데이터에 등록된 재료가 없습니다.")

    st.divider()
    st.markdown("### 조리법")
    preparation = recipe.get("preparation")
    if preparation:
        st.markdown("#### 조리 전 준비")
        st.markdown("\n".join(f"- {item}" for item in preparation))
        st.divider()
    st.markdown("#### 조리")
    for index, step in enumerate(recipe["steps"], start=1):
        if len(recipe["steps"]) > 1:
            st.markdown(f"**{step['title']}**")
        for position, column in enumerate(st.columns(3), start=1):
            photo_column = column.container(border=True)
            note = photo_note(step, position)
            name = note.get("photo")
            photo = photos.get(name)
            if photo is None and name and Path(name).name == name:
                stored_photo = recipe_file.parent / name
                if stored_photo.is_file():
                    photo = str(stored_photo)
            if photo is None:
                photo = find_photo(recipe_file.parent, f"{index:02}-{position}")
            if photo is None and position == 1:
                photo = find_photo(recipe_file.parent, f"{index:02}")
            if photo:
                photo_column.image(str(photo) if isinstance(photo, Path) else photo, use_container_width=True)
            else:
                photo_column.markdown(
                    f'<div class="recipe-photo-slot">사진 {position} 추가 예정</div>',
                    unsafe_allow_html=True,
                )
            if note.get("title"):
                photo_column.markdown(f"**{note['title']}**")
            photo_column.write(note.get("description") or "조리 설명 추가 예정")
            caution = note.get("caution", "").strip()
            if caution:
                photo_column.markdown(
                    f'<small style="color:#d32f2f">주의사항 · {escape(caution)}</small>',
                    unsafe_allow_html=True,
                )
    st.divider()
    st.markdown("### 제공법")
    st.write(recipe.get("serving_method") or "제공 방법 추가 예정")
    serving_caution = recipe.get("serving_caution", "").strip()
    if serving_caution:
        st.markdown(
            f'<small style="color:#d32f2f">주의사항 · {escape(serving_caution)}</small>',
            unsafe_allow_html=True,
        )
