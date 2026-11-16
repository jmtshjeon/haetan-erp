import streamlit as st
import pandas as pd
import sqlite3
import os
import io
import re
from pathlib import Path
from contextlib import closing

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
    conn.commit()
    conn.close()

init_db()

def get_connection():
    return sqlite3.connect(DB_FILE)

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
st.set_page_config(page_title="해탄 원가관리 시스템", page_icon="🦑", layout="wide")

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
    st.sidebar.image(str(logo), use_container_width=True)
else:
    st.sidebar.markdown("### 🏢 HAETAN F&B")

st.sidebar.markdown("---")

menu = st.sidebar.radio(
    "📂 해탄 메뉴 분석 시스템", 
    [
        "1. 메뉴 원가 종합분석표", 
        "2. 메뉴별 원가/마진 분석", 
        "레시피",
        "3. 레시피 DB 업로드", 
        "4. 식재료 DB 마스터 관리", 
        "5. 푸디스트 및 외부식재료 DB 동기화",
        "6. 📉 단가 변동 이력 분석 (History)"
    ]
)

st.sidebar.markdown("---")
input_pw = st.sidebar.text_input("🔐 관리자 비밀번호", type="password", help="데이터 업로드 및 수정을 위한 관리자 전용 비밀번호입니다.")
is_admin = bool(ADMIN_PASSWORD) and input_pw == ADMIN_PASSWORD

if is_admin:
    st.sidebar.success("✅ 관리자 모드 활성화됨")
else:
    st.sidebar.info("👀 보기 전용 모드 (수정 불가)")

st.markdown(
    """
    <div style='background-color: #f0f2f6; padding: 15px; border-radius: 10px; margin-bottom: 20px;'>
        <h1 style='text-align: center; color: #1f2937;'>📊 F&B 식자재 원가 통합 관리 대시보드</h1>
        <p style='text-align: center; color: #4b5563;'>가맹점 상권별 탄력 판가 적용 및 세트메뉴 원가 연산 시스템</p>
    </div>
    """, 
    unsafe_allow_html=True
)

# --- 기능 1: 전체 메뉴 종합 요약 분석표 ---
if menu == "1. 메뉴 원가 종합분석표":
    st.subheader("📋 메뉴 원가 종합분석표")
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
            menu_category = menu_data.iloc[0]['menu_type'] 
            sell_price = price_dict.get(m_name, 0.0) 
            
            total_cost = 0
            for _, row in menu_data.iterrows():
                ing_name = row['ingredient_name']
                qty = row['input_qty']
                unit_p = unit_prices.get(ing_name, 0.0)
                total_cost += unit_p * qty
                
            margin = sell_price - total_cost
            cost_rate = (total_cost / sell_price * 100) if sell_price > 0 else 0
            
            summary_data.append({
                "카테고리": menu_category,
                "메뉴명": m_name,
                "적용 판매가(원)": int(sell_price),
                "총 원가(원)": int(total_cost),
                "예상 마진(원)": int(margin),
                "원가율(%)": round(cost_rate, 1)
            })
            
        if summary_data:
            df_summary = pd.DataFrame(summary_data)
            
            category_order = {"최종메뉴": 1, "점심특선": 2, "추가메뉴": 3, "주류/음료": 4}
            df_summary['sort_key'] = df_summary['카테고리'].map(category_order).fillna(99)
            df_summary = df_summary.sort_values(by=['sort_key', '메뉴명']).drop(columns=['sort_key'])

            def color_cost_rate(val):
                if val >= 35.0:
                    return 'background-color: #ffe6e6; color: #cc0000; font-weight: bold;'
                elif val <= 30.0:
                    return 'background-color: #e6f2ff; color: #0066cc; font-weight: bold;'
                return ''
                
            styled_df = df_summary.style.format({
                "총 원가(원)": "{:,}",
                "예상 마진(원)": "{:,}",
                "원가율(%)": "{:.1f}"
            }).map(color_cost_rate, subset=['원가율(%)'])

            edited_df = st.data_editor(
                styled_df,
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
            
            if is_admin:
                if st.button("💾 수정한 판매가 DB에 저장하기 (클릭 필수)"):
                    conn = get_connection()
                    cursor = conn.cursor()
                    for _, row in edited_df.iterrows():
                        cursor.execute("INSERT OR REPLACE INTO menu_prices (menu_name, sell_price) VALUES (?, ?)", (row['메뉴명'], row['적용 판매가(원)']))
                    conn.commit()
                    conn.close()
                    st.cache_data.clear() # 💡 데이터 변경 시 캐시 삭제
                    st.success("판매가가 성공적으로 변경 및 재계산되었습니다.")
                    st.rerun()
        else:
            st.info("현재 등록된 판매 메뉴가 없습니다.")

# --- 기능 2: 메뉴별 원가/마진 분석 ---
elif menu == "2. 메뉴별 원가/마진 분석":
    st.subheader("📈 카테고리별 원가/마진 상세 분석")
    st.markdown("상단의 리스트에서 분석할 메뉴를 클릭하시면 아래에 상세 내역이 표시됩니다.")
    
    unit_prices, df_bom, missing_ings = calculate_unit_prices()
    
    if df_bom.empty:
        st.warning("레시피 DB가 비어있습니다.")
    else:
        conn = get_connection()
        df_prices = pd.read_sql_query("SELECT * FROM menu_prices", conn)
        conn.close()
        price_dict = dict(zip(df_prices['menu_name'], df_prices['sell_price']))

        sales_menus_df = df_bom[df_bom['menu_type'] != '프랩']
        
        category_order = ["최종메뉴", "점심특선", "추가메뉴", "주류/음료"]
        tabs = st.tabs(category_order)
        
        for tab, cat in zip(tabs, category_order):
            with tab:
                cat_menus = sales_menus_df[sales_menus_df['menu_type'] == cat]['menu_name'].unique()
                if len(cat_menus) == 0:
                    st.info(f"등록된 [{cat}] 메뉴가 없습니다.")
                else:
                    selected_m_name = st.radio(
                        f"👇 분석하실 **[{cat}]** 메뉴를 선택해주세요:",
                        sorted(cat_menus),
                        horizontal=True, 
                        key=f"radio_{cat}"
                    )
                    
                    st.markdown("---")
                    
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
                        img_folder = APP_DIR / "메뉴이미지" 
                        img_path_jpg = os.path.join(img_folder, f"{selected_m_name}.jpg")
                        img_path_png = os.path.join(img_folder, f"{selected_m_name}.png")
                        
                        if os.path.exists(img_path_jpg):
                            st.image(img_path_jpg, caption=selected_m_name, use_container_width=True)
                        elif os.path.exists(img_path_png):
                            st.image(img_path_png, caption=selected_m_name, use_container_width=True)
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
                                "투입량(g/EA)": qty,
                                "1단위당 단가(원)": unit_p,
                                "발생 원가(원)": cost
                            })
                        st.table(pd.DataFrame(recipe_details).style.format({
                            "투입량(g/EA)": "{:,.1f}", 
                            "1단위당 단가(원)": "{:,.2f}", 
                            "발생 원가(원)": "{:,.1f}"
                        }))
                    
                    if simulated_price > 0:
                        cost_rate = (total_cost / simulated_price) * 100
                        margin = simulated_price - total_cost
                        
                        st.markdown("---")
                        m1, m2, m3 = st.columns(3)
                        m1.metric(label="총 투입 원가", value=f"{int(total_cost):,} 원")
                        m2.metric(label=f"실제 마진 ({simulated_price:,}원 판매시)", value=f"{int(margin):,} 원")
                        m3.metric(label="최종 원가율", value=f"{cost_rate:.1f} %", delta=f"{30 - cost_rate:.1f}% (안정권 30% 기준)", delta_color="inverse")

# --- 기능 3: 레시피 DB 업로드 ---
elif menu == "레시피":
    from recipe_view import show_recipes

    show_recipes(st, get_connection, is_admin)

elif menu == "3. 레시피 DB 업로드":
    st.subheader("🍳 표준 메뉴 레시피(BOM) DB 관리")
    
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
                "menu_type": st.column_config.SelectboxColumn("메뉴분류", options=["프랩", "최종메뉴", "점심특선", "추가메뉴", "주류/음료"], disabled=not is_admin),
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
                conn.commit()
                conn.close()
                st.cache_data.clear() # 💡 데이터 변경 시 캐시 삭제
                st.success("레시피가 안전하게 수정되었습니다.")
                st.rerun()

# --- 기능 4: 식재료 DB 마스터 관리 ---
elif menu == "4. 식재료 DB 마스터 관리":
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
                "supply_price": st.column_config.NumberColumn("공급가(원)", disabled=not is_admin), 
                "loss_rate": st.column_config.NumberColumn("손실률(%)", disabled=not is_admin), 
                "total_capacity": st.column_config.NumberColumn("총용량(g)", disabled=not is_admin), 
                "unit": st.column_config.TextColumn("단위", disabled=True),
                "최종단가(g당)": st.column_config.NumberColumn("단위당 단가", format="%.3f 원", disabled=True)
            },
            num_rows="dynamic" if is_admin else "fixed", use_container_width=True
        )
        
        if is_admin:
            if st.button("🔄 수정된 데이터 반영"):
                cursor = conn.cursor()
                for idx, row in edited_df.iterrows():
                    cursor.execute("""
                        UPDATE ingredients SET item_name=?, tax_type=?, supply_price=?, loss_rate=?, total_capacity=?, unit='g' WHERE item_code=?
                    """, (row['item_name'].strip(), row['tax_type'], row['supply_price'], row['loss_rate'], row['total_capacity'], row['item_code']))
                conn.commit()
                st.cache_data.clear() # 💡 데이터 변경 시 캐시 삭제
                st.success("동기화 완료!")
                st.rerun()
    conn.close()

# --- 기능 5: 푸디스트 및 외부식재료 DB 동기화 ---
elif menu == "5. 푸디스트 및 외부식재료 DB 동기화":
    st.subheader("🔄 물류 단가 스마트 자동 업데이트")
    
    if not is_admin:
        st.warning("⛔ 발주서 엑셀 업로드(단가 동기화) 기능은 **관리자 전용**입니다.")
    else:
        uploaded_file = st.file_uploader("발주서 엑셀 파일 업로드", type=["xlsx", "csv"])
        
        if uploaded_file:
            df_order = pd.read_excel(uploaded_file) if not uploaded_file.name.endswith('.csv') else pd.read_csv(uploaded_file)
            if st.button("🚀 데이터 자동 파싱 및 단가 갱신"):
                conn = get_connection()
                cursor = conn.cursor()
                
                updated_items = []
                new_items = []
                update_count = 0 
                
                weight_col = next((col for col in df_order.columns if '총중량' in str(col).replace(" ", "")), None)
                qty_col = next((col for col in df_order.columns if '수량' == str(col).strip()), None)
                tax_col = next((col for col in df_order.columns if '부가세' in str(col)), None)
                price_col = next((col for col in df_order.columns if '단가' == str(col).strip()), None)
                
                if not price_col:
                    st.error("엑셀 파일에 '단가' 열이 존재하지 않습니다.")
                else:
                    for idx, row in df_order.dropna(subset=['상품코드']).iterrows():
                        code = str(row['상품코드']).strip()
                        name = str(row['상품명']).strip()
                        price = float(row[price_col]) if pd.notna(row[price_col]) else 0.0
                        
                        t_type = "면세"
                        if tax_col and pd.notna(row[tax_col]):
                            try:
                                t_type = "과세" if float(row[tax_col]) > 0 else "면세"
                            except:
                                pass
                        
                        total_weight_kg = 0.0
                        if weight_col and pd.notna(row[weight_col]):
                            try: total_weight_kg = float(row[weight_col])
                            except: pass
                            
                        qty = 1.0
                        if qty_col and pd.notna(row[qty_col]):
                            try: qty = float(row[qty_col]) if float(row[qty_col]) > 0 else 1.0
                            except: pass

                        if total_weight_kg > 0:
                            capacity = (total_weight_kg / qty) * 1000.0
                        else:
                            capacity = 1000.0
                        
                        item_unit = 'g' 
                        
                        cursor.execute("SELECT supply_price FROM ingredients WHERE item_code=?", (code,))
                        db_result = cursor.fetchone()
                        
                        if db_result:
                            old_price = db_result[0]
                            cursor.execute("UPDATE ingredients SET supply_price=?, tax_type=?, total_capacity=?, unit=? WHERE item_code=?", (price, t_type, capacity, item_unit, code))
                            update_count += 1
                            
                            if old_price != price:
                                diff = price - old_price
                                diff_str = f"🔺 +{int(diff):,}원" if diff > 0 else f"🔻 {int(diff):,}원"
                                cursor.execute("INSERT INTO price_history (item_code, item_name, old_price, new_price, change_amount) VALUES (?, ?, ?, ?, ?)", (code, name, old_price, price, diff))
                                updated_items.append({"상품명": name, "용량": f"{int(capacity)}g", "기존단가": f"{int(old_price):,}원", "변경단가": f"{int(price):,}원", "변동액": diff_str})
                        else:
                            cursor.execute("INSERT INTO ingredients (item_code, item_name, tax_type, supply_price, loss_rate, total_capacity, unit) VALUES (?, ?, ?, ?, 0.0, ?, ?)", (code, name, t_type, price, capacity, item_unit))
                            new_items.append({"상품명": name, "용량": f"{int(capacity)}g", "신규등록단가": f"{int(price):,}원"})
                            
                    conn.commit()
                    conn.close()
                    st.cache_data.clear() # 💡 데이터 변경 시 캐시 삭제
                    
                    st.success(f"데이터 스마트 분석 완료! (기존 데이터 교정: {update_count}건, 신규 등록: {len(new_items)}건)")
                    
                    col_u, col_n = st.columns(2)
                    with col_u:
                        with st.expander(f"🔄 단가 변동 내역 상세 ({len(updated_items)}건)", expanded=True):
                            if updated_items: st.dataframe(pd.DataFrame(updated_items), use_container_width=True, hide_index=True)
                            else: st.info("변동된 품목이 없습니다.")
                    with col_n:
                        with st.expander(f"🆕 신규 등록 상세 (세금/중량 매칭 결과) ({len(new_items)}건)", expanded=True):
                            if new_items: st.dataframe(pd.DataFrame(new_items), use_container_width=True, hide_index=True)
                            else: st.info("신규 추가된 품목이 없습니다.")

# --- 기능 6: 물류 단가 변동 이력 분석 ---
elif menu == "6. 📉 단가 변동 이력 분석 (History)":
    st.subheader("📉 식재료 단가 장기 변동 추이 및 이력 분석")
    conn = get_connection()
    df_hist = pd.read_sql_query("SELECT update_date, item_code, item_name, old_price, new_price, change_amount FROM price_history ORDER BY update_date DESC", conn)
    conn.close()
    
    if df_hist.empty:
        st.info("아직 단가 변동 이력이 없습니다.")
    else:
        df_hist['update_date'] = pd.to_datetime(df_hist['update_date']).dt.strftime('%Y-%m-%d %H:%M')
        search_keyword = st.text_input("특정 식재료 이름으로 검색 (예: 오징어)")
        filtered_df = df_hist[df_hist['item_name'].str.contains(search_keyword, case=False, na=False)] if search_keyword else df_hist
            
        def format_change(val): return f"🔺 +{int(val):,}원" if val > 0 else (f"🔻 {int(val):,}원" if val < 0 else "-")

        display_df = filtered_df.copy()
        display_df['변동 내역'] = display_df['change_amount'].apply(format_change)
        display_df['old_price'] = display_df['old_price'].apply(lambda x: f"{int(x):,}원")
        display_df['new_price'] = display_df['new_price'].apply(lambda x: f"{int(x):,}원")
        
        display_df = display_df.rename(columns={'update_date': '변동 일시', 'item_code': '상품코드', 'item_name': '상품명', 'old_price': '기존 단가', 'new_price': '새로운 단가'})
        st.dataframe(display_df[['변동 일시', '상품코드', '상품명', '기존 단가', '새로운 단가', '변동 내역']], use_container_width=True, hide_index=True)
