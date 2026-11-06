
from pathlib import Path
import sqlite3
import json
import pandas as pd


# =========================
# 1. 路径配置
# =========================

PROJECT_ROOT = Path(__file__).resolve().parents[1]

RAW_DIR = PROJECT_ROOT / "data" / "raw"
DB_DIR = PROJECT_ROOT / "data" / "database"

EXCEL_FILE = RAW_DIR / "BOPET_100篇论文数据汇总.xlsx"
CSV_FILE = RAW_DIR / "100篇文献元数据.csv"

DB_FILE = DB_DIR / "bopet.db"


# Excel 实际数据表及数据库表名
SHEETS = {
    "文献索引": "papers_index",
    "参数矩阵": "parameter_matrix",
    "数值证据_01-50": "evidence_01_50",
    "数值证据_51-100": "evidence_51_100",
    "表格数据": "table_data",
    "提取质量": "extraction_quality",
}


# =========================
# 2. 读取 Excel
# =========================

def load_excel():
    if not EXCEL_FILE.exists():
        raise FileNotFoundError(
            f"找不到 Excel 文件：{EXCEL_FILE}"
        )

    result = {}

    for sheet_name, table_name in SHEETS.items():

        print(f"正在读取工作表：{sheet_name}")

        # 这份 Excel 的正式表头位于第 5 行
        df = pd.read_excel(
            EXCEL_FILE,
            sheet_name=sheet_name,
            header=4
        )

        # 删除完全为空的行和列
        df = df.dropna(how="all")
        df = df.dropna(axis=1, how="all")

        # 删除无效的 Unnamed 列
        df = df.loc[
            :,
            ~df.columns.astype(str).str.startswith("Unnamed")
        ]

        # 清理列名
        df.columns = [
            str(col).strip()
            for col in df.columns
        ]

        # 删除全空记录
        df = df.reset_index(drop=True)

        result[table_name] = df

        print(
            f"  成功：{len(df)} 条记录，"
            f"{len(df.columns)} 个字段"
        )

    return result


# =========================
# 3. 读取 CSV 元数据
# =========================

def load_metadata():

    if not CSV_FILE.exists():
        raise FileNotFoundError(
            f"找不到 CSV 文件：{CSV_FILE}"
        )

    try:
        df = pd.read_csv(
            CSV_FILE,
            encoding="utf-8-sig"
        )
    except UnicodeDecodeError:
        df = pd.read_csv(
            CSV_FILE,
            encoding="gbk"
        )

    df = df.dropna(how="all")
    df = df.reset_index(drop=True)

    print(
        f"CSV 元数据读取成功：{len(df)} 条记录"
    )

    return df


# =========================
# 4. 导入 SQLite
# =========================

def save_database(tables, metadata):

    DB_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    with sqlite3.connect(DB_FILE) as conn:

        for table_name, df in tables.items():

            df.to_sql(
                table_name,
                conn,
                if_exists="replace",
                index=False
            )

            print(
                f"已写入数据库：{table_name}"
            )

        metadata.to_sql(
            "paper_metadata",
            conn,
            if_exists="replace",
            index=False
        )

        print("已写入数据库：paper_metadata")

        # 记录本次导入信息
        report = {
            name: {
                "rows": len(df),
                "columns": list(df.columns)
            }
            for name, df in tables.items()
        }

        report["paper_metadata"] = {
            "rows": len(metadata),
            "columns": list(metadata.columns)
        }

        conn.execute("""
            CREATE TABLE IF NOT EXISTS import_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_report TEXT
            )
        """)

        conn.execute(
            "INSERT INTO import_log (import_report) VALUES (?)",
            (json.dumps(report, ensure_ascii=False),)
        )

        conn.commit()


# =========================
# 5. 检查导入结果
# =========================

def check_database():

    print("\n========== 数据库检查 ==========")

    with sqlite3.connect(DB_FILE) as conn:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type='table'
            ORDER BY name
        """)

        table_names = [
            row[0]
            for row in cursor.fetchall()
        ]

        for table in table_names:

            cursor.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            )

            count = cursor.fetchone()[0]

            print(
                f"{table}: {count} 条记录"
            )

    print("\n数据库文件：")
    print(DB_FILE)


# =========================
# 6. 主程序
# =========================

def main():

    print("开始导入 BOPET 文献数据...\n")

    tables = load_excel()

    metadata = load_metadata()

    save_database(
        tables,
        metadata
    )

    check_database()

    print("\n数据导入完成！")


if __name__ == "__main__":
    main()
