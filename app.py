import streamlit as st
import pandas as pd
import sqlite3
import os
import io
import re
from pathlib import Path
from contextlib import closing
from html import escape
from recipe_view import MENU_BOARD_ORDER, find_menu_photo, menu_board_category

# --- [설정] 관리자 비밀번호 ---
ADMIN_PASSWORD = os.getenv("HAETAN_ADMIN_PASSWORD")

# --- 1. 데이터베이스(SQLite) 초기화 ---
APP_DIR = Path(__file__).resolve().parent
DB_FILE = APP_DIR / "haetan_cost_management.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ingredients (
            item_code TEXT PRIMARY KEY,
            item_name TEXT NOT NULL,
            tax_type TEXT DEFAULT '과세',
            supply_price REAL DEFAULT 0,
            loss_rate REAL DEFAULT 0,
            total_capacity REAL DEFAULT 1000,
            unit TEXT DEFAULT 'g' 
        )
    """)
    try:
        cursor.execute("ALTER TABLE ingredients ADD COLUMN unit TEXT DEFAULT 'g'")
    except sqlite3.OperationalError:
        pass 

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS menu_bom (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_type TEXT,
            menu_name TEXT,
            sell_price_or_weight REAL,
            ingredient_name TEXT,
            input_qty REAL,
            remarks TEXT DEFAULT '' 
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS menu_prices (
            menu_name TEXT PRIMARY KEY,
            sell_price REAL DEFAULT 0
        )
    """)
    
    cursor.execute("UPDATE ingredients SET item_name = TRIM(item_name)")
    cursor.execute("UPDATE menu_bom SET ingredient_name = TRIM(ingredient_name), menu_name = TRIM(menu_name)")
    cursor.execute("UPDATE menu_prices SET menu_name = TRIM(menu_name)")

    cursor.execute("SELECT menu_name, MAX(sell_price_or_weight) FROM menu_bom WHERE menu_type != '프랩' GROUP BY menu_name")
    for row in cursor.fetchall():
        cursor.execute("INSERT OR IGNORE INTO menu_prices (menu_name, sell_price) VALUES (?, ?)", (row[0], row[1]))

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_code TEXT,
            item_name TEXT,
            old_price REAL,
            new_price REAL,
            change_amount REAL,
            update_date DATETIME DEFAULT (datetime('now', 'localtime'))
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_update_dates (
            section TEXT PRIMARY KEY,
            updated_on TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_connection():
    return sqlite3.connect(DB_FILE)


def mark_cost_update(cursor):
    cursor.execute("""
        INSERT OR REPLACE INTO app_update_dates (section, updated_on)
        VALUES ('menu_cost', date('now', '+9 hours'))
    """)


def price_update_date(conn):
    return conn.execute("SELECT MAX(substr(update_date, 1, 10)) FROM price_history").fetchone()[0]


def record_price_change(cursor, code, name, old_price, new_price):
    if old_price is None or new_price is None or old_price == new_price:
        return False
    cursor.execute(
        """INSERT INTO price_history (item_code, item_name, old_price, new_price, change_amount)
           VALUES (?, ?, ?, ?, ?)""",
        (code, name, old_price, new_price, new_price - old_price),
    )
    return True

# 💡 [핵심 최적화] 데이터 연산 캐싱 적용
# 이 데코레이터가 붙은 함수는 결과값을 메모리에 저장하여 앱 멈춤 현상을 방지합니다.
@st.cache_data
def calculate_unit_prices():
    conn = get_connection()
    df_ing = pd.read_sql_query("SELECT * FROM ingredients", conn)
    prices = {}
    
    if not df_ing.empty:
        df_ing['item_name'] = df_ing['item_name'].str.strip()
        df_ing['단위당단가'] = (df_ing['supply_price'] / (1 - (df_ing['loss_rate'] / 100))) / df_ing['total_capacity']
        prices = dict(zip(df_ing['item_name'], df_ing['단위당단가']))
    
    df_bom = pd.read_sql_query("SELECT * FROM menu_bom", conn)
    missing_ingredients = set()
    
    if not df_bom.empty:
        df_bom['ingredient_name'] = df_bom['ingredient_name'].str.strip()
        df_bom['menu_name'] = df_bom['menu_name'].str.strip()
        
        all_menu_names = set(df_bom['menu_name'].unique())
        
        for ing in df_bom['ingredient_name'].unique():
            if ing not in prices and ing not in all_menu_names:
                missing_ingredients.add(ing)
                prices[ing] = 0.0 
        
        max_iterations = 7 
        for _ in range(max_iterations):
            is_updated = False
            for menu_name in all_menu_names:
                prep_items = df_bom[df_bom['menu_name'] == menu_name]
                prep_cost = 0
                is_calculable = True
                
                for _, row in prep_items.iterrows():
                    ing_name = row['ingredient_name']
                    qty = row['input_qty']
                    
                    if ing_name in prices:
                        prep_cost += prices[ing_name] * qty
                    else:
                        is_calculable = False
                        break
                        
                if is_calculable:
                    yield_weight = prep_items.iloc[0]['sell_price_or_weight']
                    if yield_weight > 0 and prep_items.iloc[0]['menu_type'] == '프랩':
                        new_price = prep_cost / yield_weight
                    else:
                        new_price = prep_cost
                        
                    if menu_name not in prices or prices[menu_name] != new_price:
                        prices[menu_name] = new_price
                        is_updated = True
            
            if not is_updated:
                break
                
    conn.close()
    return prices, df_bom, list(missing_ingredients)

# --- 2. Streamlit UI 공통 설정 ---
APP_NAME = "해탄 조리·원가"
st.set_page_config(page_title=APP_NAME, page_icon="🦑", layout="wide")

# 💡 [핵심 최적화] 세션 유지(Keep-Alive) 스크립트 삽입
# 브라우저가 열려있는 동안 1분마다 백그라운드 신호를 보내 세션 끊김을 방지합니다.
st.components.v1.html(
    """
    <script>
    setInterval(function() {
        window.parent.postMessage('keepAlive', '*');
    }, 60000);
    </script>
    """,
    height=0, width=0
)

logo = APP_DIR / "logo.png"
if not logo.is_file():
    logo = APP_DIR / "logo.png.jpg"
if logo.is_file():
    st.sidebar.image(str(logo), width=145)
st.sidebar.markdown(f"### {APP_NAME}")

st.sidebar.markdown("---")

with st.sidebar.expander("관리자 로그인"):
    input_pw = st.text_input("관리자 비밀번호", type="password")
    is_admin = bool(ADMIN_PASSWORD) and input_pw == ADMIN_PASSWORD
    if is_admin:
        st.success("관리자 모드 활성화됨")
    elif input_pw:
        st.error("관리자 비밀번호를 확인해 주세요.")

franchise_menus = [
    "해탄 메뉴 원가 및 마진표",
    "조리 매뉴얼",
    "식재료단가 · 푸디스트 기준",
]
admin_menus = ["필독 · 관리자 모드 활용", "단가 갱신", "식재료 정보 관리", "원가용 레시피 관리"]
if is_admin and not st.session_state.get("was_admin"):
    st.session_state["admin_menu"] = admin_menus[0]
    st.session_state["view_mode"] = "관리자 전용"
st.session_state["was_admin"] = is_admin
if is_admin:
    view = st.sidebar.radio("화면 선택", ["관리자 전용", "가맹점 화면"], key="view_mode")
else:
    view = "가맹점 화면"
if view == "관리자 전용":
    menu = st.sidebar.radio("[관리자 전용]", admin_menus, key="admin_menu")
else:
    menu = st.sidebar.radio("가맹점용", franchise_menus, key="franchise_menu")
    with st.sidebar.expander("보는 법"):
        st.markdown(
            "1. **원가 및 마진표**에서 메뉴 행을 눌러 상세를 확인해요. "
            "우리 매장 판매가를 입력해 원가율과 마진을 비교할 수 있어요.\n"
            "2. **조리 매뉴얼**에서 분류와 메뉴 이름을 눌러 조리법을 봐요.\n"
            "3. **식재료단가**에서 현재 단가와 변동 이력을 확인해요."
        )

if view == "가맹점 화면":
    st.markdown("""
        <style>
        .st-key-franchise-mobile-header {display:none}
        @media (max-width: 767px) {
            .st-key-franchise-mobile-header {display:flex !important}
            .st-key-franchise-mobile-header button {min-height:42px}
        }
        @media (min-width: 768px) {
            .st-key-franchise_menu label p {font-size:1rem;line-height:1.4}
            .st-key-pc-cost-heading h2 {font-size:1.9rem;letter-spacing:-.035em}
            .pc-cost-category {display:flex;align-items:center;gap:.65rem;margin-top:.6rem;
                border-left:4px solid #4263a1;padding-left:.65rem}
            .pc-cost-category h4 {padding:0;margin:0;font-size:1.2rem}
            .pc-cost-category span {font-size:.85rem;opacity:.75}
        }
        </style>
    """, unsafe_allow_html=True)
    with st.container(key="franchise-mobile-header"):
        st.markdown(f"### {APP_NAME}")
        for column, label, target in zip(
            st.columns(3), ("원가·마진", "조리 매뉴얼", "식재료단가"), franchise_menus
        ):
            column.button(
                label, key=f"mobile_nav:{target}", use_container_width=True,
                on_click=st.session_state.update, args=({"franchise_menu": target},),
                type="primary" if menu == target else "secondary",
            )

OYSTER_COST_ESTIMATES = {
    "꽃삼겹 오징어굴보쌈": (43000, 17377),
    "알배기 굴초무침": (22000, 7821),
    "통영 생굴 단품": (13000, 3050),
}


def show_oyster_costs(expanded=False):
    def rate_text(cost, price):
        tenths = (cost * 2000 + price) // (2 * price)
        return f"{tenths / 10:.1f}%"

    with st.expander("시즌 메뉴 · 통영굴 3종", expanded=expanded):
        st.caption("11월 판매 재개 · B타입 기준판매가 · 공지 원가 기준 (생굴 13,000원/kg)")
        rows = [
            {
                "메뉴": name,
                "기준판매가": f"{price:,}원",
                "참고 이론원가": f"약 {cost:,}원",
                "원가율": rate_text(cost, price),
            }
            for name, (price, cost) in OYSTER_COST_ESTIMATES.items()
        ]
        with st.container(key="cost-desktop-oyster"):
            st.table(pd.DataFrame(rows))
        with st.container(key="cost-mobile-oyster"):
            mobile_rows = [
                {
                    "메뉴명": name, "적용 판매가(원)": price, "총 원가(원)": cost,
                    "예상 마진(원)": price - cost,
                    "원가율(%)": float(rate_text(cost, price).rstrip("%")),
                }
                for name, (price, cost) in OYSTER_COST_ESTIMATES.items()
            ]
            st.markdown(mobile_cost_list(pd.DataFrame(mobile_rows)), unsafe_allow_html=True)
        name = st.radio("판매가를 비교할 굴 메뉴", list(OYSTER_COST_ESTIMATES), horizontal=True)
        photo = find_menu_photo(name)
        if photo:
            st.image(str(photo), caption=name, width=280)
        price, cost = OYSTER_COST_ESTIMATES[name]
        actual = st.number_input(
            "우리 매장 판매가(원)", min_value=0, value=price, step=1000,
            key=f"oyster_price:{name}",
        )
        if actual > 0:
            rate, margin = st.columns(2)
            rate.metric("원가율", rate_text(cost, actual))
            margin.metric("판매가 − 참고 이론원가", f"{actual - cost:,}원")


def mobile_cost_list(frame):
    cards = []
    for row in frame.to_dict("records"):
        name = escape(str(row["메뉴명"]))
        price = row["적용 판매가(원)"]
        rate = row["원가율(%)"]
        margin = row["예상 마진(원)"]
        price_text = f"{price:,}원" if price > 0 else "판매가 미등록"
        rate_text = f"{rate:.1f}%" if pd.notna(rate) else "—"
        margin_text = f"{margin:,.0f}원" if pd.notna(margin) else "—"
        cards.append(
            '<details class="mobile-cost-card">'
            f'<summary><strong>{name}</strong><span>{price_text}<b>원가율 {rate_text}</b></span></summary>'
            f'<div>총 원가 <strong>{row["총 원가(원)"]:,}원</strong></div>'
            f'<div>예상 마진 <strong>{margin_text}</strong></div></details>'
        )
    return "".join(cards)


def show_menu_cost_detail(selected_m_name, df_bom, price_dict, unit_prices):
    menu_data = df_bom[df_bom['menu_name'] == selected_m_name]
    default_price = int(price_dict.get(selected_m_name, 0))

    st.markdown(f"#### 🎯 [{selected_m_name}] 가맹점 상권별 판매가 시뮬레이터")

    simulated_price = st.number_input(
        f"실제 판매가 입력 (현재 적용가: {default_price:,}원)", 
        min_value=0, value=default_price, step=1000,
        format="%d", 
        key=f"sim_{selected_m_name}"
    )

    st.markdown("<br>", unsafe_allow_html=True)

    img_col, data_col = st.columns([1, 2])

    with img_col:
        photo = find_menu_photo(selected_m_name)
        if photo:
            st.image(str(photo), caption=selected_m_name, use_container_width=True)
        else:
            st.markdown(
                f"<div style='background-color:#e5e7eb; height:200px; display:flex; align-items:center; justify-content:center; border-radius:10px;'><p style='color:#6b7280; text-align:center; font-size:14px; margin:0;'>📷 이미지가 없습니다</p></div>", 
                unsafe_allow_html=True
            )

    with data_col:
        total_cost = 0
        recipe_details = []
        for _, row in menu_data.iterrows():
            ing_name = row['ingredient_name']
            qty = row['input_qty']
            remarks_text = row['remarks'] if pd.notna(row['remarks']) else ""
            unit_p = unit_prices.get(ing_name, 0.0) 
            cost = unit_p * qty
            total_cost += cost

            recipe_details.append({
                "식재료/프랩명": ing_name,
                "비고(설명)": remarks_text,
                "투입량": qty,
                "1단위당 단가(원)": unit_p,
                "발생 원가(원)": cost
            })
        st.table(pd.DataFrame(recipe_details).style.format({
            "투입량": "{:,.1f}", 
            "1단위당 단가(원)": lambda value: f"{int(value):,}",
            "발생 원가(원)": lambda value: f"{int(value):,}"
        }))

    if simulated_price > 0:
        cost_rate = (total_cost / simulated_price) * 100
        margin = simulated_price - total_cost

        st.markdown("---")
        m1, m2, m3 = st.columns(3)
        m1.metric(label="총 투입 원가", value=f"{int(total_cost):,} 원")
        m2.metric(label=f"실제 마진 ({simulated_price:,}원 판매시)", value=f"{int(margin):,} 원")
        m3.metric(label="최종 원가율", value=f"{cost_rate:.1f} %", delta=f"{30 - cost_rate:.1f}% (안정권 30% 기준)", delta_color="inverse")


# --- 기능 1: 전체 메뉴 종합 요약 분석표 ---
if menu == "필독 · 관리자 모드 활용":
    st.subheader("필독 · 관리자 모드 활용")
    st.markdown("""
**평소에는 ‘단가 갱신’만 열면 됩니다.**

1. 푸디스트에서 **해탄 원가관리용 즐겨찾기 엑셀**을 내려받아요.
2. **단가 갱신**에 올려 기존 단가와 조회 단가를 비교해요. 포장 규격이 달라졌거나 기준이 불분명한 품목은 선택을 해제해요.
3. 선택한 품목을 반영하고 **가맹점 화면 → 식재료단가 · 푸디스트 기준 → 변동 이력**에서 확인해요.

**업무가 바뀐 경우에만 수정해요.**

- 시장·직발주 단가, 포장용량, 손실률은 **식재료 정보 관리**에서 수정해요.
- 메뉴에 쓰는 재료·투입량이나 프랩 완성중량은 **원가용 레시피 관리**에서 수정해요. 전체 파일을 올릴 때는 전체 메뉴와 프랩이 들어 있는지 확인해요.
- 본사 기준판매가는 **가맹점 화면 → 해탄 메뉴 원가 및 마진표 → 기준 판매가 수정**에서 저장해요. 메뉴 상세의 ‘우리 매장 판매가’는 비교용이에요.
- 조리 설명과 사진은 **가맹점 화면 → 조리 매뉴얼 → 레시피 수정**에서 미리 본 뒤 저장해요.

**담당자가 바뀌면**

본사 공용 위치에 최신 원가 DB, 기준 엑셀, 레시피와 사진, 마지막 반영일을 남겨요. 새 담당자와 대체 담당자에게 보관 위치와 갱신 순서를 알려주고, 한 번 직접 갱신해 보도록 해요. 관리자 접근 설정은 인수인계 시 본사에서 관리해요.

웹 앱에서 저장한 데이터와 사진은 재배포 후 보존이 보장되지 않아요. 운영에 쓰는 최신 파일은 본사 공용 위치에 별도로 보관해요.
""")

elif menu == "해탄 메뉴 원가 및 마진표":
    with st.container(key="pc-cost-heading"):
        st.subheader("해탄 메뉴 원가 및 마진표")
    with closing(get_connection()) as conn:
        saved_date = conn.execute(
            "SELECT updated_on FROM app_update_dates WHERE section='menu_cost'"
        ).fetchone()
        dates = [date for date in (saved_date[0] if saved_date else None, price_update_date(conn)) if date]
    st.caption(f"업데이트 날짜 · {max(dates) if dates else '기록 없음'}")
    if is_admin:
        st.markdown("전체 메뉴의 마진을 분석합니다. **판매가(원)를 더블클릭하여 수정한 뒤 반드시 아래의 [저장하기] 버튼을 누르세요.**")
    else:
        st.markdown("전체 메뉴의 마진 분석표입니다. (판매가 수정은 관리자만 가능합니다)")
    
    unit_prices, df_bom, missing_ings = calculate_unit_prices()
    
    if missing_ings:
        st.warning(f"⚠️ **[알림] 마스터 DB에 없어서 0원으로 임시 연산된 식재료:** {', '.join(missing_ings)}")

    if df_bom.empty:
        st.warning("레시피 DB가 비어있습니다.")
    else:
        conn = get_connection()
        df_prices = pd.read_sql_query("SELECT * FROM menu_prices", conn)
        conn.close()
        price_dict = dict(zip(df_prices['menu_name'], df_prices['sell_price']))

        sales_menus_df = df_bom[df_bom['menu_type'] != '프랩']
        final_menus = sales_menus_df['menu_name'].unique()
        
        summary_data = []
        
        for m_name in final_menus:
            menu_data = df_bom[df_bom['menu_name'] == m_name]
            menu_category = menu_board_category(menu_data.iloc[0]['menu_type'], m_name) 
            if menu_category is None:
                continue
            sell_price = price_dict.get(m_name, 0.0) 
            
            total_cost = 0
            for _, row in menu_data.iterrows():
                ing_name = row['ingredient_name']
                qty = row['input_qty']
                unit_p = unit_prices.get(ing_name, 0.0)
                total_cost += unit_p * qty
                
            margin = sell_price - total_cost
            cost_rate = (total_cost / sell_price * 100) if sell_price > 0 else None
            
            summary_data.append({
                "카테고리": menu_category,
                "메뉴명": m_name,
                "적용 판매가(원)": int(sell_price),
                "총 원가(원)": int(total_cost),
                "예상 마진(원)": int(margin) if sell_price > 0 else None,
                "원가율(%)": round(cost_rate, 1) if cost_rate is not None else None
            })
            
        if summary_data:
            df_summary = pd.DataFrame(summary_data)
            
            category_order = {category: index for index, category in enumerate(MENU_BOARD_ORDER)}
            df_summary['sort_key'] = df_summary['카테고리'].map(category_order).fillna(99)
            df_summary = df_summary.sort_values(by=['sort_key', '메뉴명']).drop(columns=['sort_key'])

            def color_cost_rate(val):
                if pd.isna(val):
                    return ""
                if val >= 35.0:
                    return 'background-color: #ffe6e6; color: #cc0000; font-weight: bold;'
                elif val <= 30.0:
                    return 'background-color: #e6f2ff; color: #0066cc; font-weight: bold;'
                return ''
                
            st.markdown("""
                <style>
                [class*="st-key-cost-mobile-"] {display:none}
                @media (max-width: 767px) {
                    [class*="st-key-cost-desktop-"] {display:none !important}
                    [class*="st-key-cost-mobile-"] {display:flex !important}
                    .mobile-cost-card {border:1px solid rgba(128,128,128,.3);border-radius:10px;margin:0 0 .5rem;padding:.7rem .8rem}
                    .mobile-cost-card summary {cursor:pointer;min-height:44px;overflow-wrap:anywhere;font-size:1rem}
                    .mobile-cost-card summary span {display:flex;justify-content:space-between;gap:.5rem;margin-top:.25rem;font-size:.92rem}
                    .mobile-cost-card div {display:flex;justify-content:space-between;gap:.5rem;padding-top:.4rem}
                }
                </style>
            """, unsafe_allow_html=True)
            with st.container(key="cost-desktop-help"):
                st.caption("표에서 메뉴를 선택하면 아래에 원가 상세와 판매가 비교가 열립니다.")
            with st.container(key="cost-mobile-help"):
                st.caption("메뉴 이름을 누르면 원가와 마진이 펼쳐집니다.")
            with st.container(key="cost-mobile-simulator"):
                with st.expander("우리 매장 판매가로 비교"):
                    name = st.selectbox("메뉴 선택", df_summary["메뉴명"].tolist(), key="mobile_cost_menu")
                    starting_price = int(price_dict.get(name, 0))
                    price = st.number_input("우리 매장 판매가(원)", min_value=0, value=starting_price,
                                            step=1000, key=f"mobile_sim:{name}")
                    exact_cost = unit_prices.get(name)
                    if exact_cost is not None:
                        st.write(f"총 원가 **{int(exact_cost):,}원**")
                        if price > 0:
                            st.write(f"원가율 **{exact_cost / price:.1%}** · 예상 마진 **{int(price - exact_cost):,}원**")
            for group, visible in df_summary.groupby("카테고리", sort=False):
                st.markdown(
                    f'<div class="pc-cost-category"><h4>{escape(group)}</h4>'
                    f'<span>메뉴 {len(visible)}개</span></div>',
                    unsafe_allow_html=True,
                )
                visible = visible.reset_index(drop=True)
                with st.container(key=f"cost-mobile-list-{group}"):
                    st.markdown(mobile_cost_list(visible), unsafe_allow_html=True)
                styled_df = visible.drop(columns=["카테고리"]).style.format({
                    "적용 판매가(원)": lambda value: f"{value:,}" if value > 0 else "미등록",
                    "총 원가(원)": "{:,}",
                    "예상 마진(원)": "{:,.0f}",
                    "원가율(%)": "{:.1f}",
                }, na_rep="—").map(color_cost_rate, subset=["원가율(%)"]).set_properties(
                    subset=["메뉴명"], **{"font-weight": "bold"}
                )
                with st.container(key=f"cost-desktop-table-{group}"):
                    selection = st.dataframe(
                        styled_df, hide_index=True, use_container_width=True,
                        on_select="rerun", selection_mode="single-row", key=f"cost_overview:{group}",
                        height=43 + 42 * len(visible), row_height=42,
                        column_config={
                            "메뉴명": st.column_config.TextColumn("메뉴명", width=240),
                            "적용 판매가(원)": st.column_config.Column("판매가 (원)", width=115),
                            "총 원가(원)": st.column_config.Column("이론원가 (원)", width=115),
                            "예상 마진(원)": st.column_config.Column("예상 마진 (원)", width=115),
                            "원가율(%)": st.column_config.Column("원가율 (%)", width=95),
                        },
                    )
                    if selection.selection.rows:
                        row_index = selection.selection.rows[0]
                        if row_index < len(visible):
                            show_menu_cost_detail(visible.iloc[row_index]["메뉴명"], df_bom, price_dict, unit_prices)

            if is_admin:
                with st.expander("기준 판매가 수정"):
                    edited_df = st.data_editor(
                        df_summary,
                        column_config={
                            "카테고리": st.column_config.TextColumn("분류", disabled=True),
                            "메뉴명": st.column_config.TextColumn("메뉴명", disabled=True),
                            "적용 판매가(원)": st.column_config.NumberColumn("적용 판매가(원)", disabled=not is_admin, format="%d"),
                            "총 원가(원)": st.column_config.TextColumn("총 원가(원)", disabled=True),
                            "예상 마진(원)": st.column_config.TextColumn("예상 마진(원)", disabled=True),
                            "원가율(%)": st.column_config.TextColumn("원가율(%)", disabled=True)
                        },
                        use_container_width=True, hide_index=True
                    )
                    if st.button("💾 수정한 판매가 DB에 저장하기 (클릭 필수)"):
                        conn = get_connection()
                        cursor = conn.cursor()
                        for _, row in edited_df.iterrows():
                            cursor.execute("INSERT OR REPLACE INTO menu_prices (menu_name, sell_price) VALUES (?, ?)", (row['메뉴명'], row['적용 판매가(원)']))
                        mark_cost_update(cursor)
                        conn.commit()
                        conn.close()
                        st.cache_data.clear() # 💡 데이터 변경 시 캐시 삭제
                        st.success("판매가가 성공적으로 변경 및 재계산되었습니다.")
                        st.rerun()
        else:
            st.info("현재 등록된 판매 메뉴가 없습니다.")

    show_oyster_costs(expanded=True)

# --- 기능 2: 메뉴별 원가/마진 분석 ---
elif menu == "조리 매뉴얼":
    from recipe_view import show_recipes

    show_recipes(st, get_connection, is_admin)

elif menu == "원가용 레시피 관리":
    st.subheader("원가용 레시피 관리")
    
    if not is_admin:
        st.info("⚠️ 레시피 파일 업로드 및 데이터 덮어쓰기는 **관리자만 가능**합니다. (좌측 메뉴에서 비밀번호 입력)")
    
    st.markdown("💡 판매용 메뉴의 '프랩완성중량(판매가)' 칸에 **판매가격(예: 26000)**을 적으면 초기 가격으로 셋팅됩니다.")
    
    template_df = pd.DataFrame({
        '메뉴분류': ['최종메뉴', '프랩', '점심특선'],
        '메뉴명': ['돌판오징어(2인)', '해탄 비법소스', '해탄 점심특선'],
        '프랩완성중량(판매가)': [26000, 5000, 12000],
        '구성_식재료명': ['오징어', '고춧가루', '돌판오징어(2인)'],
        '투입량(g/EA)': [300, 500, 1],
        '비고': ['고정 판매가를 적으면 자동연동', '프랩은 완성무게(g) 입력', '']
    })
    
    csv_buffer = template_df.to_csv(index=False).encode('utf-8-sig')
    st.download_button(
        label="📥 [클릭] 신규 카테고리/판매가 반영 엑셀(CSV) 양식 다운로드",
        data=csv_buffer,
        file_name="해탄_레시피_카테고리및판매가적용양식.csv",
        mime="text/csv",
    )
    
    if is_admin:
        with st.expander("📂 엑셀 파일로 레시피 일괄 덮어쓰기 (관리자 전용)"):
            uploaded_bom = st.file_uploader("메뉴 레시피 엑셀(CSV) 업로드", type=["xlsx", "csv"])
            if uploaded_bom:
                try:
                    df_upload = pd.read_excel(uploaded_bom) if uploaded_bom.name.lower().endswith(".xlsx") else pd.read_csv(uploaded_bom)
                    df_upload.columns = df_upload.columns.str.strip()
                    df_upload = df_upload.dropna(how="all")
                    qty_col = next((col for col in ("투입량(g)", "투입량(g/EA)") if col in df_upload), None)
                    weight_col = next((col for col in ("프랩완성중량(판매가)", "프랩완성중량(g)", "판매가(완성중량)") if col in df_upload), None)
                    required = ["메뉴분류", "메뉴명", "구성_식재료명"]
                    if df_upload.empty or not qty_col or not weight_col or not all(col in df_upload for col in required):
                        raise ValueError("레시피 표준양식의 열 이름과 데이터가 있는지 확인해 주세요.")
                    if (df_upload[required].fillna("").astype(str).apply(lambda col: col.str.strip()) == "").any().any():
                        raise ValueError("메뉴분류, 메뉴명, 구성 식재료명이 비어 있는 행을 확인해 주세요.")
                    quantities = pd.to_numeric(df_upload[qty_col], errors="coerce")
                    raw_weights = df_upload[weight_col].fillna(0)
                    weights = pd.to_numeric(raw_weights, errors="coerce")
                    if (quantities.isna() | (quantities < 0) | (quantities == float("inf"))).any():
                        raise ValueError("투입량을 0 이상의 숫자로 입력해 주세요.")
                    if (weights.isna() | (weights < 0) | (weights == float("inf"))).any():
                        raise ValueError("프랩 완성중량과 판매가를 0 이상의 숫자로 입력해 주세요.")
                    df_upload = df_upload.assign(**{qty_col: quantities, weight_col: weights})
                    if "비고" not in df_upload:
                        df_upload["비고"] = ""
                except ValueError as error:
                    st.error(f"레시피 파일 확인 필요 · {error}")
                except Exception:
                    st.error("레시피 파일을 열 수 없습니다. 엑셀 또는 CSV 파일을 확인해 주세요.")
                else:
                    st.caption(f"반영 대상 {len(df_upload):,}줄")
                    if st.button("⚠️ 기존 레시피를 모두 지우고 이 엑셀 데이터로 덮어쓰기"):
                        with closing(get_connection()) as conn, conn:
                            cursor = conn.cursor()
                            cursor.execute("DELETE FROM menu_bom")
                            for _, row in df_upload.iterrows():
                                remarks_val = str(row['비고']) if pd.notna(row['비고']) else ""
                                menu_cat = str(row['메뉴분류']).strip()
                                m_name = str(row['메뉴명']).strip()
                                ing_name = str(row['구성_식재료명']).strip() 
                                w_val = float(row[weight_col]) if pd.notna(row[weight_col]) else 0.0
                        
                                if menu_cat != '프랩':
                                    if w_val > 0:
                                        cursor.execute("INSERT OR REPLACE INTO menu_prices (menu_name, sell_price) VALUES (?, ?)", (m_name, w_val))
                                    else:
                                        cursor.execute("INSERT OR IGNORE INTO menu_prices (menu_name, sell_price) VALUES (?, 0)", (m_name,))

                                cursor.execute("""
                                    INSERT INTO menu_bom (menu_type, menu_name, sell_price_or_weight, ingredient_name, input_qty, remarks)
                                    VALUES (?, ?, ?, ?, ?, ?)
                                """, (menu_cat, m_name, w_val, ing_name, row[qty_col], remarks_val))
                            mark_cost_update(cursor)
                        st.cache_data.clear()
                        st.rerun()

    conn = get_connection()
    df_bom_view = pd.read_sql_query("SELECT id, menu_type, menu_name, sell_price_or_weight, ingredient_name, input_qty, remarks FROM menu_bom", conn)
    conn.close()

    if not df_bom_view.empty:
        st.markdown("#### 📋 현재 등록된 레시피 상세 내역")
        edited_bom = st.data_editor(
            df_bom_view,
            column_config={
                "id": None, 
                "menu_type": st.column_config.SelectboxColumn("메뉴분류", options=["프랩", "최종메뉴", "점심특선", "추가메뉴", "사이드", "주류/음료"], disabled=not is_admin),
                "menu_name": st.column_config.TextColumn("메뉴명", disabled=not is_admin),
                "sell_price_or_weight": st.column_config.NumberColumn("프랩완성중량(판매가)", format="%d", disabled=not is_admin),
                "ingredient_name": st.column_config.TextColumn("구성 식재료명", disabled=not is_admin),
                "input_qty": st.column_config.NumberColumn("투입량(g/EA)", format="%.1f", disabled=not is_admin),
                "remarks": st.column_config.TextColumn("비고", disabled=not is_admin)
            },
            num_rows="dynamic" if is_admin else "fixed", use_container_width=True
        )
        if is_admin:
            if st.button("저장: 수정된 레시피 표 내용 반영하기"):
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM menu_bom")
                for _, row in edited_bom.iterrows():
                    if pd.notna(row['menu_name']) and pd.notna(row['ingredient_name']):
                        remarks_val = str(row['remarks']) if pd.notna(row['remarks']) else ""
                        m_name = str(row['menu_name']).strip()
                        menu_cat = str(row['menu_type']).strip()
                        ing_name = str(row['ingredient_name']).strip()
                        w_val = float(row['sell_price_or_weight'])
                        
                        if menu_cat != '프랩':
                            if w_val > 0:
                                cursor.execute("INSERT OR REPLACE INTO menu_prices (menu_name, sell_price) VALUES (?, ?)", (m_name, w_val))
                            else:
                                cursor.execute("INSERT OR IGNORE INTO menu_prices (menu_name, sell_price) VALUES (?, 0)", (m_name,))
                                
                        cursor.execute("""
                            INSERT INTO menu_bom (menu_type, menu_name, sell_price_or_weight, ingredient_name, input_qty, remarks)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (menu_cat, m_name, w_val, ing_name, row['input_qty'], remarks_val))
                mark_cost_update(cursor)
                conn.commit()
                conn.close()
                st.cache_data.clear() # 💡 데이터 변경 시 캐시 삭제
                st.success("레시피가 안전하게 수정되었습니다.")
                st.rerun()

# --- 기능 4: 식재료 DB 마스터 관리 ---
elif menu == "식재료 정보 관리":
    st.subheader("📝 식재료 마스터 DB 관리")
    
    if is_admin:
        with st.expander("➕ 신규 식재료 개별 등록 (수동)"):
            with st.form("new_item_form"):
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    item_code = st.text_input("상품코드")
                    item_name = st.text_input("상품명")
                with col2:
                    tax_type = st.selectbox("세금 유형", ["과세", "면세"])
                    supply_price = st.number_input("공급가(원)", min_value=0.0, step=100.0)
                with col3:
                    loss_rate = st.number_input("손실률(%)", min_value=0.0, max_value=100.0, step=0.1)
                    total_capacity = st.number_input("총용량(g)", min_value=1.0, value=1000.0)
                with col4:
                    unit_type = st.selectbox("단위", ["g", "EA", "ml"]) 
                
                if st.form_submit_button("식재료 DB에 추가"):
                    if item_code and item_name:
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute("""
                            INSERT OR REPLACE INTO ingredients (item_code, item_name, tax_type, supply_price, loss_rate, total_capacity, unit)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (item_code.strip(), item_name.strip(), tax_type, supply_price, loss_rate, total_capacity, unit_type))
                        conn.commit()
                        conn.close()
                        st.cache_data.clear() # 💡 데이터 변경 시 캐시 삭제
                        st.success(f"[{item_name}] 저장 완료!")
                        st.rerun()
                    else:
                        st.error("상품코드와 상품명을 모두 입력해주세요.")
    else:
        st.info("💡 식재료 신규 등록 및 수정은 **관리자 모드**에서만 가능합니다.")

    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM ingredients", conn)
    
    if not df.empty:
        df['수율적용단가'] = df['supply_price'] / (1 - (df['loss_rate'] / 100))
        df['최종단가(g당)'] = df['수율적용단가'] / df['total_capacity']
        
        search_query = st.text_input("🔍 식재료 검색 (상품명 또는 코드)", placeholder="검색어를 입력하세요")
        display_df = df[df['item_name'].str.contains(search_query, case=False, na=False) | df['item_code'].str.contains(search_query, case=False, na=False)] if search_query else df
            
        edited_df = st.data_editor(
            display_df[['item_code', 'item_name', 'tax_type', 'supply_price', 'loss_rate', 'total_capacity', 'unit', '최종단가(g당)']],
            column_config={
                "item_code": st.column_config.TextColumn("상품코드", disabled=True), 
                "item_name": st.column_config.TextColumn("상품명", disabled=not is_admin), 
                "tax_type": st.column_config.TextColumn("세금유형", disabled=not is_admin),
                "supply_price": st.column_config.NumberColumn("공급가(원)", format="%d 원", disabled=not is_admin), 
                "loss_rate": st.column_config.NumberColumn("손실률(%)", disabled=not is_admin), 
                "total_capacity": st.column_config.NumberColumn("총용량(g)", disabled=not is_admin), 
                "unit": st.column_config.TextColumn("단위", disabled=True),
                "최종단가(g당)": st.column_config.NumberColumn("단위당 단가", format="%d 원", disabled=True)
            },
            num_rows="dynamic" if is_admin else "fixed", use_container_width=True
        )
        
        if is_admin:
            if st.button("🔄 수정된 데이터 반영"):
                with closing(conn), conn:
                    cursor = conn.cursor()
                    for _, row in edited_df.iterrows():
                        code = row["item_code"]
                        name = row["item_name"].strip()
                        price = float(row["supply_price"]) if pd.notna(row["supply_price"]) else None
                        old = cursor.execute(
                            "SELECT supply_price FROM ingredients WHERE item_code=?", (code,)
                        ).fetchone()
                        if old is None:
                            continue
                        if price is None:
                            price = old[0]
                        cursor.execute("""
                            UPDATE ingredients SET item_name=?, tax_type=?, supply_price=?, loss_rate=?, total_capacity=? WHERE item_code=?
                        """, (name, row["tax_type"], price, row["loss_rate"], row["total_capacity"], code))
                        record_price_change(cursor, code, name, old[0], price)
                    mark_cost_update(cursor)
                st.cache_data.clear() # 💡 데이터 변경 시 캐시 삭제
                st.success("동기화 완료!")
                st.rerun()
    conn.close()

# --- 기능 5: 푸디스트 및 외부식재료 DB 동기화 ---
elif menu == "단가 갱신":
    st.subheader("단가 갱신")
    if is_admin:
        from price_update import show_price_update

        show_price_update(st, get_connection, record_price_change)

elif menu == "식재료단가 · 푸디스트 기준":
    st.subheader("식재료단가 · 푸디스트 기준")
    with closing(get_connection()) as conn:
        price_date = price_update_date(conn)
    st.caption(f"기준날짜 · {price_date or '기록 없음'}")
    current, history = st.tabs(["현재 단가", "변동 이력"])
    with current:
        with closing(get_connection()) as conn:
            ingredients = pd.read_sql_query("""
                WITH latest AS (
                    SELECT item_code, old_price, new_price,
                           ROW_NUMBER() OVER (PARTITION BY item_code ORDER BY update_date DESC, id DESC) AS seq
                    FROM price_history
                )
                SELECT i.item_code AS 상품코드, i.item_name AS 식재료,
                       i.supply_price AS 기준단가, i.total_capacity AS 포장용량, i.unit AS 단위,
                       i.supply_price - latest.old_price AS 변동액,
                       100.0 * (i.supply_price - latest.old_price) / NULLIF(latest.old_price, 0) AS "변동률"
                FROM ingredients AS i
                LEFT JOIN latest ON i.item_code = latest.item_code AND latest.seq = 1
                    AND latest.new_price = i.supply_price
                WHERE i.item_code GLOB 'F[0-9]*'
                ORDER BY i.item_name
            """, conn)
        keyword = st.text_input("식재료명 또는 상품코드 검색", key="ingredient_price_search").strip()
        if keyword:
            match = ingredients[["식재료", "상품코드"]].apply(
                lambda col: col.str.contains(keyword, case=False, na=False, regex=False)
            )
            ingredients = ingredients[match.any(axis=1)]
        st.dataframe(ingredients.style.format({
                         "기준단가": lambda value: f"{int(value):,}원", "포장용량": "{:,.0f}",
                         "변동액": lambda value: f"{int(value):+,}원", "변동률": "{:+.1f}%",
                     }, na_rep="—"),
                     hide_index=True, use_container_width=True)
    with history:
        conn = get_connection()
        df_hist = pd.read_sql_query("SELECT update_date, item_code, item_name, old_price, new_price, change_amount FROM price_history ORDER BY update_date DESC, id DESC", conn)
        conn.close()
    
        if df_hist.empty:
            st.info("이 데이터에 저장된 단가 변경 이력이 없습니다. 기존 품목의 가격이 실제로 바뀐 경우에 기록됩니다.")
        else:
            df_hist['update_date'] = pd.to_datetime(df_hist['update_date'], errors="coerce").dt.strftime('%Y-%m-%d %H:%M')
            search_keyword = st.text_input("상품명 또는 상품코드로 검색").strip()
            filtered_df = df_hist
            if search_keyword:
                match = df_hist[["item_name", "item_code"]].apply(
                    lambda col: col.str.contains(search_keyword, case=False, na=False, regex=False)
                )
                filtered_df = df_hist[match.any(axis=1)]
            st.caption(f"변동 이력 {len(filtered_df):,}건")
            
            def format_change(val): return f"🔺 +{int(val):,}원" if val > 0 else (f"🔻 {int(val):,}원" if val < 0 else "-")

            display_df = filtered_df.copy()
            display_df['변동 내역'] = display_df['change_amount'].apply(format_change)
            display_df['old_price'] = display_df['old_price'].apply(lambda x: f"{int(x):,}원")
            display_df['new_price'] = display_df['new_price'].apply(lambda x: f"{int(x):,}원")
        
            display_df = display_df.rename(columns={'update_date': '변동 일시', 'item_code': '상품코드', 'item_name': '상품명', 'old_price': '기존 단가', 'new_price': '새로운 단가'})
            st.dataframe(display_df[['변동 일시', '상품코드', '상품명', '기존 단가', '새로운 단가', '변동 내역']], use_container_width=True, hide_index=True)
