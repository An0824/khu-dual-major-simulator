import streamlit as st
import pandas as pd
import numpy as np
import re
import sqlite3
import tempfile
import os

# ---------------------------------------------------------
# 1. 유틸리티 함수 (오리지널 복원)
# ---------------------------------------------------------
def get_base_code(text):
    if pd.isna(text) or text == "": return ""
    raw = re.sub(r'[^a-zA-Z0-9]', '', str(text)).upper()
    match = re.search(r'([A-Z]+)([0-9]+)', raw)
    if match:
        prefix, digits = match.group(1), match.group(2)
        if len(digits) == 5: return prefix + digits[:3]
        elif len(digits) >= 6: return prefix + digits[:4]
        else: return prefix + digits
    return raw[:8]

def clean_name(text):
    if pd.isna(text): return ""
    text = str(text)
    text = re.sub(r'\(👉.*?\)', '', text)
    return re.sub(r'[^가-힣a-zA-Z0-9]', '', text).strip().lower()

def calculate_khu_gpa(df):
    if df.empty: return 0.0
    # P/NP만 제외하고 F는 포함하여 평점 계산
    gpa_df = df[~df['성적'].isin(['P', 'NP'])].dropna(subset=['평점', '학점'])
    if gpa_df['학점'].sum() == 0: return 0.0
    return (gpa_df['평점'] * gpa_df['학점']).sum() / gpa_df['학점'].sum()

def load_from_db(uploaded_db):
    """SQLite DB에서 데이터를 읽어 기존 엑셀 호환 DataFrame으로 반환"""
    tmp_path = os.path.join(tempfile.gettempdir(), 'khu_planner_temp.db')
    with open(tmp_path, 'wb') as f:
        f.write(uploaded_db.read())
    uploaded_db.seek(0)

    conn = sqlite3.connect(tmp_path)

    programs = pd.read_sql("SELECT * FROM programs", conn)
    programs['시트명'] = programs.apply(
        lambda r: f"{r['year']}_{r['department']}({r['track']})", axis=1
    )

    grad_req_db = pd.read_sql("""
        SELECT p.year||'_'||p.department||'('||p.track||')' as "시트명(전공트랙)",
               g.전공요구, g.전공기초, g.전공필수, g.전공선택, g.총졸업요구,
               g.타전공인정최대, g.중복인정최대, g.비고,
               g.타전공인정범위, g.타전공인정대상
        FROM graduation_rules g
        JOIN programs p ON g.program_id = p.program_id
    """, conn)

    detail_req_db = pd.read_sql("""
        SELECT p.year||'_'||p.department||'('||p.track||')' as "시트명(전공트랙)",
               d.요건타입, d.필수그룹명, d.요구과목수, d.요구학점, d.비고
        FROM detail_rules d
        JOIN programs p ON d.program_id = p.program_id
    """, conn)

    alias_db = pd.read_sql("""
        SELECT e.원본코드, e.원본과목명, e.대체코드, e.대체과목명,
               p.year||'_'||p.department||'('||p.track||')' as "적용대상",
               e.비고
        FROM equivalents e
        JOIN programs p ON e.program_id = p.program_id
    """, conn)

    all_curriculum = pd.read_sql("""
        SELECT p.year||'_'||p.department||'('||p.track||')' as "시트명",
               cur.이수구분, c.과목코드, c.과목명, c.학점,
               cur.필수그룹, cur.타전공인정여부, cur.비고
        FROM curriculum cur
        JOIN courses c ON cur.과목코드 = c.과목코드
        JOIN programs p ON cur.program_id = p.program_id
    """, conn)

    conn.close()
    return programs, all_curriculum, grad_req_db, detail_req_db, alias_db

# ---------------------------------------------------------
# 2. 성적표 파싱 엔진
# ---------------------------------------------------------
def process_academic_records(uploaded_file, db_p, db_d, alias_db, m1_sheet, m2_sheet):
    try:
        df = pd.read_csv(uploaded_file, encoding="cp949", skiprows=4, header=None)
    except (UnicodeDecodeError, UnicodeError):
        uploaded_file.seek(0)
        df = pd.read_csv(uploaded_file, encoding="utf-8-sig", skiprows=4, header=None)
    
    combined_db = pd.concat([db_p, db_d]) if db_d is not None else db_p
    cols = [str(c).strip() for c in combined_db.columns]
    code_idx = next((i for i, c in enumerate(cols) if '번호' in c or '코드' in c), 0)
    code_col = combined_db.columns[code_idx]
    
    n_idx = next((i for i, c in enumerate(cols) if '과목명' in c or '교과목명' in c), 1)
    n_col = combined_db.columns[n_idx]
    
    cat_col = next((c for c in combined_db.columns if '이수구분' in str(c)), None)
    
    db_lookup = combined_db.assign(c=combined_db[code_col].apply(get_base_code)).drop_duplicates('c').set_index('c', drop=False).to_dict('index')

    records = []
    grade_re = re.compile(r'^(A\+|A0|A-|B\+|B0|B-|C\+|C0|C-|D\+|D0|D-|F|P|NP)$', re.IGNORECASE)
    
    current_semester_label = "1-1"
    regular_sem_count = 0 
    
    for _, row in df.iterrows():
        cells = [str(c).strip() for c in row if pd.notna(c) and str(c).strip() != '']
        if not cells: continue
        
        sem_str = next((c for c in cells if re.search(r'(1학기|2학기|여름학기|겨울학기)', c)), None)
        
        if sem_str:
            if '1학기' in sem_str:
                regular_sem_count += 1
                year = (regular_sem_count - 1) // 2 + 1
                current_semester_label = f"{year}-1" if year <= 4 else f"초과-{year}-1"
            elif '2학기' in sem_str:
                regular_sem_count += 1
                year = (regular_sem_count - 1) // 2 + 1
                current_semester_label = f"{year}-2" if year <= 4 else f"초과-{year}-2"
            elif '여름' in sem_str or '겨울' in sem_str:
                current_semester_label = "계절"

        code_idx_list = [i for i, c in enumerate(cells) if re.search(r'[A-Z]{2,}[0-9]{3,}', c)]
        
        
        if code_idx_list:
            idx = code_idx_list[0]
            b_code = get_base_code(cells[idx])
            name = cells[idx + 1] if idx + 1 < len(cells) else "알수없음"
            
            grade, nums = "", []
            for c in cells[idx+2:]:
                if grade_re.match(c.upper()): grade = c.upper()
                elif re.match(r'^[0-9](\.[0-9]+)?$', c): nums.append(float(c))
            
            credit = nums[0] if len(nums) > 0 else 0.0
            point = nums[1] if len(nums) > 1 else (0.0 if grade == 'F' else np.nan)
            
            # --- 내부 연산용 대체과목(eff_code) 탐색 ---
            eff_code = b_code
            if alias_db is not None and not alias_db.empty:
                for _, a_row in alias_db.iterrows():
                    target_str = str(a_row.get('적용대상', a_row.get('적용대상 (콤마 지원!)', '')))
                    targets = [t.strip() for t in target_str.split(',')] if target_str and target_str != 'nan' else []
                    
                    apply_m1 = not targets or any(t in m1_sheet for t in targets)
                    apply_m2 = m2_sheet and (not targets or any(t in m2_sheet for t in targets))
                    
                    if apply_m1 or apply_m2:
                        src = get_base_code(a_row['원본코드'])
                        dst = get_base_code(a_row['대체코드'])
                        src_name = clean_name(a_row['원본과목명'])
                        c_name = clean_name(name)
                        
                        if b_code == src or (c_name and c_name == src_name):
                            eff_code = dst
                            break
            
            # DB를 참조하여 이수구분(cat)을 정밀하게 가져오는 로직
            cat = db_lookup[eff_code][cat_col] if eff_code in db_lookup and cat_col else ("일반선택(타전공)" if "전공" in str(cells) else "교양/기타")
            
            if eff_code != b_code and eff_code in db_lookup:
                target_name = db_lookup[eff_code][n_col]
                if "(👉" not in name:
                    name = f"{name} (👉 {target_name} 인정)"
                if "대체" not in str(cat):
                    cat = f"{cat} ✨대체인정"
            
            records.append({'학기': current_semester_label, '과목코드': b_code, '과목명': name, '이수구분': cat, '학점': credit, '평점': point, '성적': grade, 'eff_code': eff_code})
            
    return pd.DataFrame(records)

# ---------------------------------------------------------
# 3. 메인 앱 실행부
# ---------------------------------------------------------
def main():
    st.set_page_config(page_title="KHU Smart Planner", layout="wide")
    st.title("🎓 경희대 다전공 시뮬레이터")

    with st.sidebar:
        st.header("📂 데이터 업로드")
        db_file = st.file_uploader("1. 마스터 DB (khu_planner.db)", type=["db"])
        user_file = st.file_uploader("2. 성적표 (CSV)", type="csv")
        
        if db_file:
            programs, all_curriculum, grad_req_db, detail_req_db, alias_db = load_from_db(db_file)
            
            years = sorted(programs['year'].unique().tolist(), reverse=True)
            sel_year = st.selectbox("📅 적용 교육과정(연도)", years)
            
            year_programs = programs[programs['year'] == sel_year]
            available_majors = [f"{r['department']}({r['track']})" for _, r in year_programs.iterrows()]
            
            m1_name = st.selectbox("🥇 제1전공", available_majors)
            m2_name = st.selectbox("🥈 복수전공", ["없음"] + available_majors)
            
            m1_sheet = f"{sel_year}_{m1_name}"
            m2_sheet = f"{sel_year}_{m2_name}" if m2_name != "없음" else None

        st.divider()
        st.header("💾 진행 상황 저장 / 불러오기")
        
        # 1. 진행 상황 다운로드 (세이브) - 시뮬레이터가 켜진 상태(plans 존재)일 때만 버튼 표시
        if 'plans' in st.session_state:
            import json
            # Dataframe들을 딕셔너리로 변환
            save_data = {sem: df.to_dict(orient='records') for sem, df in st.session_state['plans'].items()}
            json_str = json.dumps(save_data, ensure_ascii=False, indent=2)
            
            st.download_button(
                label="📥 현재 시뮬레이션 결과 저장 (세이브 파일)",
                data=json_str,
                file_name="my_khu_plan.json",
                mime="application/json",
                use_container_width=True
            )
            st.caption("다운로드한 파일을 나중에 아래에 업로드하면 복구됩니다.")

        save_file = st.file_uploader("📤 세이브 파일 불러오기 (.json)", type="json")
        if save_file is not None:
            save_id = save_file.name + str(save_file.size)
            if st.session_state.get('_last_loaded_save') != save_id:
                try:
                    import json
                    loaded_data = json.load(save_file)
                    if 'plans' in st.session_state:
                        for sem, records in loaded_data.items():
                            if sem in st.session_state['plans']:
                                st.session_state['plans'][sem] = pd.DataFrame(records)
                        
                        # 에디터 위젯 캐시 삭제 (동기화/초기화 버튼과 동일한 처리)
                        for sem in st.session_state['plans']:
                            widget_key = f"ed_{sem}"
                            if widget_key in st.session_state:
                                del st.session_state[widget_key]
                        
                        st.session_state['_last_loaded_save'] = save_id
                        st.success("🎉 저장된 데이터를 성공적으로 불러왔습니다!")
                        st.rerun()
                except Exception as e:
                    st.error("파일을 읽는 중 오류가 발생했습니다. 올바른 세이브 파일인지 확인해 주세요.")

    if db_file and user_file:
        # grad_req_db, alias_db, detail_req_db, all_curriculum은 사이드바에서 이미 로드됨
        
        m1_db = all_curriculum[all_curriculum['시트명'] == m1_sheet].drop(columns=['시트명']).copy()
        m1_db['과목코드'] = m1_db['과목코드'].apply(get_base_code)
        
        m2_db = None
        if m2_sheet:
            m2_db = all_curriculum[all_curriculum['시트명'] == m2_sheet].drop(columns=['시트명']).copy()
            m2_db['과목코드'] = m2_db['과목코드'].apply(get_base_code)

        combined_db = pd.concat([m1_db, m2_db]) if m2_db is not None else m1_db

        m1_codes = m1_db['과목코드'].tolist()
        m2_codes = m2_db['과목코드'].tolist() if m2_db is not None else []

        req_m1 = grad_req_db[grad_req_db['시트명(전공트랙)'] == m1_sheet].iloc[0]
        req_m2 = grad_req_db[grad_req_db['시트명(전공트랙)'] == m2_sheet].iloc[0] if m2_sheet else None

        # 타전공인정 화이트리스트 생성 (3가지 범위 지원)
        m1_인정범위 = str(req_m1.get('타전공인정범위', '') or '')
        if m1_인정범위 == '전체':
            m1_whitelist = [c for c in all_curriculum[all_curriculum['시트명'] != m1_sheet]['과목코드'].apply(get_base_code).unique().tolist() if c not in m1_codes]
        elif m1_인정범위 == '학과지정':
            target_depts = [d.strip() for d in str(req_m1.get('타전공인정대상', '') or '').split(',') if d.strip()]
            m1_whitelist = [c for c in all_curriculum[all_curriculum['시트명'].apply(lambda s: any(d in s for d in target_depts))]['과목코드'].apply(get_base_code).unique().tolist() if c not in m1_codes]
        else:  # '지정' 또는 None → 기존 방식
            m1_whitelist = m1_db[m1_db['타전공인정여부'] == 'O']['과목코드'].tolist() if '타전공인정여부' in m1_db.columns else []

        if m2_sheet and req_m2 is not None:
            m2_인정범위 = str(req_m2.get('타전공인정범위', '') or '')
            if m2_인정범위 == '전체':
                m2_whitelist = [c for c in all_curriculum[all_curriculum['시트명'] != m2_sheet]['과목코드'].apply(get_base_code).unique().tolist() if c not in m2_codes]
            elif m2_인정범위 == '학과지정':
                target_depts = [d.strip() for d in str(req_m2.get('타전공인정대상', '') or '').split(',') if d.strip()]
                m2_whitelist = [c for c in all_curriculum[all_curriculum['시트명'].apply(lambda s: any(d in s for d in target_depts))]['과목코드'].apply(get_base_code).unique().tolist() if c not in m2_codes]
            else:
                m2_whitelist = m2_db[m2_db['타전공인정여부'] == 'O']['과목코드'].tolist() if '타전공인정여부' in m2_db.columns else []
        else:
            m2_whitelist = []

        past_df = process_academic_records(user_file, m1_db, m2_db, alias_db, m1_sheet, m2_sheet)
        
        semesters = ["1-1", "1-2", "2-1", "2-2", "3-1", "3-2", "4-1", "4-2", "계절"]
        current_file_id = user_file.name + str(user_file.size)
        
        # ✨ 1. 세션 초기화 시 9행으로 늘리고 '이수구분' 컬럼 추가!
        if 'plans' not in st.session_state or st.session_state.get('last_file_id') != current_file_id:
            st.session_state['last_file_id'] = current_file_id
            st.session_state['plans'] = {sem: pd.DataFrame([{"이수구분": "", "과목코드": "", "과목명": "", "학점": 0.0, "예상성적": "A+"}] * 9) for sem in semesters}
            
            if not past_df.empty:
                for sem in semesters:
                    sem_data = past_df[past_df['학기'] == sem]
                    if not sem_data.empty:
                        plan_rows = []
                        for _, row in sem_data.iterrows():
                            grade_val = row['성적'] if row['성적'] in ["A+", "A0", "A-", "B+", "B0", "B-", "C+", "C0", "C-", "D+", "D0", "D-", "F", "P", "NP"] else "A+"
                            plan_rows.append({
                                "이수구분": row.get('이수구분', ''), # 원본의 이수구분 가져오기
                                "과목코드": row['과목코드'], "과목명": row['과목명'], "학점": float(row['학점']),
                                "예상성적": grade_val
                            })
                        # 부족한 행은 9행까지 넉넉하게 채우기
                        while len(plan_rows) < 9: plan_rows.append({"이수구분": "", "과목코드": "", "과목명": "", "학점": 0.0, "예상성적": "A+"})
                        st.session_state['plans'][sem] = pd.DataFrame(plan_rows)

        # --- UI: 내 성적표 원본 조회 ---
        st.divider()
        with st.expander("📚 내 성적표 원본 조회 (클릭해서 열기/닫기)"):
            view_mode = st.radio("보기 방식 선택", ["🗓️ 학기별 보기", "🎓 전공/학과별 보기"], horizontal=True)
            if view_mode == "🗓️ 학기별 보기":
                unique_sems = past_df['학기'].unique()
                if len(unique_sems) > 0:
                    past_tabs = st.tabs(list(unique_sems))
                    for i, sem in enumerate(unique_sems):
                        with past_tabs[i]:
                            st.dataframe(past_df[past_df['학기'] == sem][['이수구분', '과목코드', '과목명', '학점', '성적']], use_container_width=True, hide_index=True)
            else:
                view_df = past_df.copy()
                def categorize_tab_by_eff(eff):
                    if eff in m1_codes or eff in m1_whitelist: return m1_name
                    if m2_sheet and (eff in m2_codes or eff in m2_whitelist): return m2_name
                    return "교양/기타"
                
                view_df['분류'] = view_df['eff_code'].apply(categorize_tab_by_eff)
                cats = [m1_name] + ([m2_name] if m2_sheet else []) + ["교양/기타"]
                cat_tabs = st.tabs(cats)
                for i, cat in enumerate(cats):
                    with cat_tabs[i]:
                        cat_df = view_df[view_df['분류'] == cat]
                        st.dataframe(cat_df[['학기', '이수구분', '과목코드', '과목명', '학점', '성적']], use_container_width=True, hide_index=True)
                        st.caption(f"✨ 총 **{cat_df['학점'].sum():.1f} 학점** 이수")

        # --- UI: 시뮬레이터 ---
        st.divider()
        st.subheader("🗓️ 전 학기 수강 시뮬레이터")
        st.caption("💡 기이수 성적이 자동으로 채워져 있습니다. 성적을 수정하거나 미래 학기 과목을 입력해 보세요.")

        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
            if st.button("🔄 과목코드/이름으로 DB 동기화", use_container_width=True):
                lookup_code = combined_db.assign(c_code=combined_db['과목코드'].apply(get_base_code)).set_index('c_code', drop=False).to_dict('index')
                lookup_name = combined_db.assign(c_name=combined_db['과목명'].apply(clean_name)).set_index('c_name', drop=False).to_dict('index')
                
                for sem in semesters:
                    df_sem = st.session_state['plans'][sem].copy()
                    for idx, row in df_sem.iterrows():
                        i_code = get_base_code(str(row['과목코드']))
                        i_name = clean_name(str(row['과목명']))
                        
                        if not i_code and not i_name: continue
                        
                        target = None
                        alias_matched = False
                        alias_src_code = None
                        alias_src_name = None
                        
                        if i_name and i_name in lookup_name: target = lookup_name[i_name]
                        elif i_code and i_code in lookup_code: target = lookup_code[i_code]
                        
                        if not target and alias_db is not None:
                            for _, a_row in alias_db.iterrows():
                                src = get_base_code(a_row['원본코드'])
                                dst = get_base_code(a_row['대체코드'])
                                src_name = clean_name(a_row['원본과목명'])
                                
                                if i_code == src or (i_name and i_name == src_name):
                                    if dst in lookup_code: 
                                        target = lookup_code[dst]
                                        alias_matched = True
                                        alias_src_code = src
                                        alias_src_name = a_row['원본과목명']
                                    break

                        if target:
                            if alias_matched:
                                if not str(row['과목코드']).strip(): df_sem.at[idx, '과목코드'] = alias_src_code
                                if not str(row['과목명']).strip(): df_sem.at[idx, '과목명'] = alias_src_name
                                # ✨ 2. 동기화 시 이수구분 자동 업데이트 (대체과목)
                                df_sem.at[idx, '이수구분'] = str(target.get('이수구분', '')) + " ✨대체인정"
                            else:
                                if not str(row['과목코드']).strip(): df_sem.at[idx, '과목코드'] = target['과목코드']
                                if not str(row['과목명']).strip(): df_sem.at[idx, '과목명'] = target['과목명']
                                # ✨ 2. 동기화 시 이수구분 자동 업데이트 (일반과목)
                                df_sem.at[idx, '이수구분'] = target.get('이수구분', '')
                            
                            if float(row['학점']) == 0: df_sem.at[idx, '학점'] = float(target['학점'])
                    st.session_state['plans'][sem] = df_sem
                    
                    widget_key = f"ed_{sem}"
                    if widget_key in st.session_state:
                        del st.session_state[widget_key]
                        
                st.rerun()

        with btn_col2:
            if st.button("🗑️ 빈칸으로 초기화 (전체 삭제)", type="primary", use_container_width=True):
                # ✨ 3. 초기화 시에도 9행 및 이수구분 반영
                st.session_state['plans'] = {sem: pd.DataFrame([{"이수구분": "", "과목코드": "", "과목명": "", "학점": 0.0, "예상성적": "A+"}] * 9) for sem in semesters}
                for sem in semesters:
                    widget_key = f"ed_{sem}"
                    if widget_key in st.session_state:
                        del st.session_state[widget_key]
                st.rerun()

        # 에디터 UI 설정 (이수구분은 사용자가 자유롭게 수정할 수도 있도록 TextColumn으로 설정)
        column_cfg = {
            "이수구분": st.column_config.TextColumn("이수구분"),
            "예상성적": st.column_config.SelectboxColumn("예상성적", options=["A+", "A0", "A-", "B+", "B0", "B-", "C+", "C0", "C-", "D+", "D0", "D-", "F", "P", "NP"], required=True),
            "학점": st.column_config.NumberColumn("학점", min_value=0.0, max_value=21.0, step=0.5)
        }
        
        tabs = st.tabs(semesters)
        for i, sem in enumerate(semesters):
            with tabs[i]:
                st.session_state['plans'][sem] = st.data_editor(st.session_state['plans'][sem], key=f"ed_{sem}", num_rows="dynamic", column_config=column_cfg)

        # --- 데이터 취합 및 내부 연산 엔진 ---
        plan_list = []
        grade_map = {'A+':4.3, 'A0':4.0, 'A-':3.7, 'B+':3.3, 'B0':3.0, 'B-':2.7, 'C+':2.3, 'C0':2.0, 'C-':1.7, 'D+':1.3, 'D0':1.0, 'D-':0.7, 'F':0.0, 'P':0.0, 'NP':0.0}
        
        for sem in semesters:
            for _, row in st.session_state['plans'][sem].iterrows():
                if str(row['과목명']).strip() or str(row['과목코드']).strip():
                    c_code = get_base_code(row['과목코드'])
                    c_name = str(row['과목명']).strip()
                    c_cat = str(row.get('이수구분', '')).strip()
                    
                    eff_code = c_code
                    if alias_db is not None and not alias_db.empty:
                        for _, a_row in alias_db.iterrows():
                            target_str = str(a_row.get('적용대상', a_row.get('적용대상 (콤마 지원!)', '')))
                            targets = [t.strip() for t in target_str.split(',')] if target_str and target_str != 'nan' else []
                            
                            apply_m1 = not targets or any(t in m1_sheet for t in targets)
                            apply_m2 = m2_sheet and (not targets or any(t in m2_sheet for t in targets))
                            
                            if apply_m1 or apply_m2:
                                src = get_base_code(a_row['원본코드'])
                                dst = get_base_code(a_row['대체코드'])
                                src_name = clean_name(a_row['원본과목명'])
                                cl_name = clean_name(c_name)
                                
                                if c_code == src or (cl_name and cl_name == src_name):
                                    eff_code = dst
                                    break

                    cat = "교양/기타"
                    if eff_code in m1_codes: cat = m1_name
                    elif m2_db is not None and eff_code in m2_codes: cat = m2_name
                    elif eff_code in m1_whitelist: cat = m1_name
                    elif m2_db is not None and eff_code in m2_whitelist: cat = m2_name
                    
                    display_name = c_name
                    if eff_code != c_code and eff_code in combined_db['과목코드'].values:
                        target_name = combined_db[combined_db['과목코드'] == eff_code]['과목명'].values[0]
                        if "(👉" not in display_name:
                            display_name = f"{display_name} (👉 {target_name} 인정)"
                    
                    plan_list.append({
                        '학기': sem, '과목코드': c_code, '과목명': display_name, '이수구분': cat, 
                        '에디터표시_이수구분': c_cat, # 화면 출력용
                        '학점': float(row['학점']), '평점': grade_map.get(row['예상성적'], 0.0), 
                        '성적': row['예상성적'], 'eff_code': eff_code
                    })
        
        sim_df = pd.DataFrame(plan_list)
        if sim_df.empty: sim_df = pd.DataFrame(columns=['학기', '이수구분', '과목코드', '과목명', '학점', '성적', 'eff_code', '평점', '에디터표시_이수구분'])
        
        passed_df = sim_df[~sim_df['성적'].isin(['F', 'NP'])].copy() if not sim_df.empty else pd.DataFrame(columns=['eff_code', '학점'])

        m1_earned = passed_df[passed_df['eff_code'].isin(m1_codes)]['학점'].sum()
        m1_extra = passed_df[(~passed_df['eff_code'].isin(m1_codes)) & (passed_df['eff_code'].isin(m1_whitelist))]['학점'].sum()
        m1_extra_applied = min(m1_extra, req_m1['타전공인정최대']) if pd.notna(req_m1['타전공인정최대']) else 0
        curr_m1_credits = m1_earned + m1_extra_applied

        curr_m2_credits = 0.0
        overlap_applied = 0.0
        if m2_db is not None:
            m2_earned = passed_df[passed_df['eff_code'].isin(m2_codes)]['학점'].sum()
            m2_extra = passed_df[(~passed_df['eff_code'].isin(m2_codes)) & (passed_df['eff_code'].isin(m2_whitelist))]['학점'].sum()
            m2_extra_applied = min(m2_extra, req_m2['타전공인정최대']) if pd.notna(req_m2['타전공인정최대']) else 0
            curr_m2_credits = m2_earned + m2_extra_applied
            
            overlap_codes = set(m1_codes) & set(m2_codes)
            actual_overlap = passed_df[passed_df['eff_code'].isin(overlap_codes)]['학점'].sum()
            limit = min(req_m1['중복인정최대'], req_m2['중복인정최대']) if pd.notna(req_m1['중복인정최대']) and pd.notna(req_m2['중복인정최대']) else 0
            overlap_applied = min(actual_overlap, limit)

        curr_total_credits = passed_df['학점'].sum() 
        REQ_TOTAL = max(req_m1['총졸업요구'], req_m2['총졸업요구']) if m2_sheet else req_m1['총졸업요구']
        REQ_M1 = req_m1['전공요구']
        REQ_M2 = req_m2['전공요구'] if m2_sheet else 0

# --- UI: 졸업 요건 달성 현황 ---
        st.divider()
        st.subheader("✅ 졸업 요건 달성 현황 (시뮬레이션 실시간 반영)")

        req_cols = st.columns(3 if m2_sheet else 2)
        
        # 1. 총 졸업 학점 컬럼
        with req_cols[0]:
            st.markdown(f"**🎓 총 졸업 학점 ({curr_total_credits:.1f} / {REQ_TOTAL})**")
            st.progress(min(curr_total_credits / REQ_TOTAL, 1.0) if REQ_TOTAL > 0 else 0)
            
        # 2. 제1전공 달성 현황 및 세부 표 컬럼
        with req_cols[1]:
            st.markdown(f"**🥇 {m1_name} ({curr_m1_credits:.1f} / {REQ_M1})**")
            st.progress(min(curr_m1_credits / REQ_M1, 1.0) if REQ_M1 > 0 else 0)
            if m1_extra_applied > 0: st.caption(f"✨ 타전공 인정 {m1_extra_applied}학점 포함")
            
            # --- ✨ 제1전공 세부 이수 내역(전공기초, 전필, 전선) 계산 로직 ---
            m1_passed = passed_df[passed_df['eff_code'].isin(m1_codes)].copy()
            # 💡 기존의 '이수구분' 컬럼을 제거하고 DB와 병합하여 충돌 방지!
            m1_merged = m1_passed.drop(columns=['이수구분'], errors='ignore').merge(
                m1_db[['과목코드', '이수구분']], left_on='eff_code', right_on='과목코드', how='left'
            )
            m1_grouped = m1_merged.groupby('이수구분')['학점'].sum()
            
            detail_data = []
            for cat in ['전공기초', '전공필수', '전공선택']:
                req_val = req_m1.get(cat, 0.0)
                if pd.notna(req_val) and req_val > 0: # 요구 학점이 있는 항목만 표시
                    earned = m1_grouped.get(cat, 0.0)
                    
                    if cat == '전공선택' and m1_extra_applied > 0:
                        earned += m1_extra_applied
                    
                    status = "✅" if earned >= req_val else "⏳"
                    detail_data.append({
                        "구분": cat, 
                        "이수 / 요구": f"{earned:.1f} / {req_val:.1f}", 
                        "달성률 (%)": f"{min(earned/req_val*100, 100):.1f}",
                        "상태": status
                    })
            if detail_data:
                st.dataframe(pd.DataFrame(detail_data), hide_index=True, use_container_width=True)

        # 3. 제2전공(복수전공) 달성 현황 및 세부 표 컬럼 (선택한 경우에만)
        if m2_sheet:
            with req_cols[2]:
                st.markdown(f"**🥈 {m2_name} ({curr_m2_credits:.1f} / {REQ_M2})**")
                st.progress(min(curr_m2_credits / REQ_M2, 1.0) if REQ_M2 > 0 else 0)
                if overlap_applied > 0: st.caption(f"🤝 중복 인정 {overlap_applied}학점 포함")

                # --- ✨ 제2전공 세부 이수 내역 계산 로직 ---
                m2_passed = passed_df[passed_df['eff_code'].isin(m2_codes)].copy()
                # 💡 마찬가지로 중복 컬럼 충돌 방지!
                m2_merged = m2_passed.drop(columns=['이수구분'], errors='ignore').merge(
                    m2_db[['과목코드', '이수구분']], left_on='eff_code', right_on='과목코드', how='left'
                )
                m2_grouped = m2_merged.groupby('이수구분')['학점'].sum()
                
                m2_detail_data = []
                for cat in ['전공기초', '전공필수', '전공선택']:
                    req_val = req_m2.get(cat, 0.0)
                    if pd.notna(req_val) and req_val > 0:
                        earned = m2_grouped.get(cat, 0.0)
                        
                        if cat == '전공선택' and (m2_extra_applied > 0 or overlap_applied > 0):
                            earned += m2_extra_applied + overlap_applied
                        
                        status = "✅" if earned >= req_val else "⏳"
                        m2_detail_data.append({
                            "구분": cat, 
                            "이수 / 요구": f"{earned:.1f} / {req_val:.1f}", 
                            "달성률 (%)": f"{min(earned/req_val*100, 100):.1f}",
                            "상태": status
                        })
                if m2_detail_data:
                    st.dataframe(pd.DataFrame(m2_detail_data), hide_index=True, use_container_width=True)

        # --- UI: 세부요건(N택M) 경고창 ---
        st.subheader("⚠️ 세부 이수 요건(그룹 필수) 체크")
        relevant_rules = detail_req_db[detail_req_db['시트명(전공트랙)'].isin([m1_sheet, m2_sheet])]
        if not relevant_rules.empty:
            for _, rule in relevant_rules.iterrows():
                if pd.isna(rule['필수그룹명']): continue
                
                req_type = str(rule.get('요건타입', 'N택M'))
                g_name = rule['필수그룹명']
                
                target_db = m1_db if rule['시트명(전공트랙)'] == m1_sheet else m2_db
                g_codes = target_db[target_db['필수그룹'] == g_name]['과목코드'].tolist()
                
                taken_count = len(passed_df[passed_df['eff_code'].isin(g_codes)])
                
                if "N택M" in req_type:
                    if taken_count < rule['요구과목수']:
                        st.error(f"**[{g_name}]** 미달: {rule['요구과목수']}과목 중 {taken_count}과목 이수 (부족: {int(rule['요구과목수']-taken_count)}과목)")
                    else:
                        st.success(f"**[{g_name}]** 충족 완료: {taken_count}과목 이수!")
        else:
            st.info("현재 선택된 전공에 등록된 세부 이수 요건이 없습니다.")

        # --- UI: GPA 추이 및 상세 표 ---
        st.divider()
        st.subheader("📈 학기별 성적(GPA) 추이")
        trend_data = []
        for sem in semesters:
            sem_df = sim_df[sim_df['학기'] == sem]
            if not sem_df.empty and len(sem_df[~sem_df['성적'].isin(['P', 'NP'])]) > 0:
                trend_data.append({'학기': sem, 'GPA': calculate_khu_gpa(sem_df)})
                
        if trend_data:
            trend_df = pd.DataFrame(trend_data).set_index('학기')
            st.line_chart(trend_df, y='GPA', use_container_width=True)
        else:
            st.info("GPA를 계산할 수 있는 학기 데이터가 없습니다.")

        st.divider()
        st.subheader("📊 졸업 시뮬레이션 상세 결과")
        c1, c2, c3 = st.columns(3)
        
        real_curr_gpa = calculate_khu_gpa(past_df[~past_df['성적'].isin(['F', 'NP'])]) if not past_df.empty else 0.0
        est_gpa = calculate_khu_gpa(sim_df[~sim_df['성적'].isin(['F', 'NP'])])
        
        c1.metric("시뮬레이션 총 이수 학점", f"{curr_total_credits:.1f}")
        c2.metric("실제 원본 GPA", f"{real_curr_gpa:.3f}")
        c3.metric("시뮬레이션 반영 예상 GPA", f"{est_gpa:.3f}", delta=f"{est_gpa - real_curr_gpa:.3f}")

        def label_category(row):
            eff = row['eff_code']
            if eff in m1_codes: return m1_name
            if m2_sheet and eff in m2_codes: return m2_name
            if eff in m1_whitelist or (m2_sheet and eff in m2_whitelist): return "타전공인정"
            return "교양/기타"
            
        sim_display = sim_df.copy()
        sim_display['이수구분'] = sim_display.apply(label_category, axis=1)
        # 에디터에 있는 이수구분을 사용자가 썼으면 그걸 놔두고, 없으면 프로그램이 내부 계산한 값 씀
        sim_display['출력용_이수구분'] = sim_display['에디터표시_이수구분'].where(sim_display['에디터표시_이수구분'] != '', sim_display['이수구분'])
        
        st.dataframe(sim_display[['학기', '출력용_이수구분', '과목코드', '과목명', '학점', '성적']].rename(columns={'출력용_이수구분': '이수구분'}), use_container_width=True, hide_index=True)
        
        # --- UI: 전체 시뮬레이션 결과 조회 (원본 + 시뮬레이터 추가분) ---
        st.divider()
        with st.expander("🔮 전체 시뮬레이션 결과 조회 (원본 + 추가 과목) (클릭해서 열기/닫기)", expanded=True):
            sim_view_mode = st.radio("전체 보기 방식 선택", ["🗓️ 학기별 보기", "🎓 전공/학과별 보기"], horizontal=True, key="sim_view")
            
            if sim_view_mode == "🗓️ 학기별 보기":
                sim_unique_sems = sim_display['학기'].unique()
                if len(sim_unique_sems) > 0:
                    # 학기별로 탭 생성
                    sim_past_tabs = st.tabs(list(sim_unique_sems))
                    for i, sem in enumerate(sim_unique_sems):
                        with sim_past_tabs[i]:
                            # 빈 과목(이름과 코드가 없는 행)은 제외하고 출력
                            sem_df = sim_display[(sim_display['학기'] == sem) & (sim_display['과목명'] != '')]
                            st.dataframe(sem_df[['출력용_이수구분', '과목코드', '과목명', '학점', '성적']].rename(columns={'출력용_이수구분': '이수구분'}), use_container_width=True, hide_index=True)
                            st.caption(f"📌 해당 학기 총 **{sem_df['학점'].sum():.1f} 학점**")
            
            else:
                # 전공/학과별 보기 탭 생성 (타전공인정도 포함)
                cats = [m1_name] + ([m2_name] if m2_sheet else []) + ["타전공인정", "교양/기타"]
                sim_cat_tabs = st.tabs(cats)
                
                for i, cat in enumerate(cats):
                    with sim_cat_tabs[i]:
                        # 내부적으로 분류된 '이수구분'을 기준으로 필터링하되, 빈 과목은 제외
                        cat_df = sim_display[(sim_display['이수구분'] == cat) & (sim_display['과목명'] != '')]
                        st.dataframe(cat_df[['학기', '출력용_이수구분', '과목코드', '과목명', '학점', '성적']].rename(columns={'출력용_이수구분': '이수구분'}), use_container_width=True, hide_index=True)
                        st.caption(f"✨ 총 **{cat_df['학점'].sum():.1f} 학점** (예상)")
    else:
        st.info("좌측 사이드바에서 마스터 DB(.db)와 성적표(CSV)를 업로드하면 시뮬레이션이 시작됩니다.")

if __name__ == "__main__":
    main()