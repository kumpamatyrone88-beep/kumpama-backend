import os
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd
from sqlalchemy import create_engine, text

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///kumpama_budget.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)

CATEGORY_ALIAS_MAP = {
    "Daily Bread": "Daily Bread",
    "Bread": "Daily Bread",
    "Daily Maveggie": "Maveggie / Fresh Produce",
    "Maveggie": "Maveggie / Fresh Produce",
    "Daily Meat": "Fresh Meat (Tue/Thu/Sat)",
    "Meat": "Fresh Meat (Tue/Thu/Sat)",
}


class Expense(BaseModel):
    user_name: str
    category_name: str
    amount: float
    description: str = ""


class CategoryUpdate(BaseModel):
    monthly_limit: float


def init_db():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                user_id SERIAL PRIMARY KEY,
                name VARCHAR(50) UNIQUE NOT NULL
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS categories (
                category_id SERIAL PRIMARY KEY,
                name VARCHAR(100) UNIQUE NOT NULL,
                bucket_type VARCHAR(50),
                monthly_limit NUMERIC(10, 2) NOT NULL
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id SERIAL PRIMARY KEY,
                amount NUMERIC(10, 2) NOT NULL,
                user_id INTEGER REFERENCES users(user_id),
                category_id INTEGER REFERENCES categories(category_id),
                description VARCHAR(255),
                date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))

        # Default users
        conn.execute(text("""
            INSERT INTO users (name) VALUES ('Silas'), ('Mercy'), ('Tyrone'), ('Nkosilathi')
            ON CONFLICT (name) DO NOTHING;
        """))

        # Default categories
        default_cats = [
            ('Rent', 'Fixed Needs', 120.00),
            ('Home Wi-Fi', 'Fixed Needs', 45.00),
            ("Mom's Transport", 'Fixed Needs', 8.00),
            ('Monthly Bulk Groceries', 'Living Expenses', 133.90),
            ('Daily Bread', 'Living Expenses', 30.00),
            ('Fresh Meat (Tue/Thu/Sat)', 'Living Expenses', 36.00),
            ('Maveggie / Fresh Produce', 'Living Expenses', 16.00),
            ('Tyrone Allowance', 'Allowances', 50.00),
            ('Nkosilathi Allowance', 'Allowances', 40.00),
            ('Unplanned Snacks & Extras', 'Flex', 0.00)
        ]
        for name, b_type, limit in default_cats:
            conn.execute(text("""
                INSERT INTO categories (name, bucket_type, monthly_limit)
                VALUES (:name, :b_type, :limit)
                ON CONFLICT (name) DO NOTHING;
            """), {"name": name, "b_type": b_type, "limit": limit})


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/api/dashboard")
def get_dashboard_stats():
    now = datetime.now()
    cur_year = now.year
    cur_month = now.month

    with engine.connect() as conn:
        cat_sql = text("""
            SELECT c.category_id, c.name, c.bucket_type, CAST(c.monthly_limit AS FLOAT) as monthly_limit,
                   COALESCE(SUM(CAST(t.amount AS FLOAT)), 0) as spent
            FROM categories c
            LEFT JOIN transactions t ON c.category_id = t.category_id 
                 AND EXTRACT(YEAR FROM t.date) = :year 
                 AND EXTRACT(MONTH FROM t.date) = :month
            GROUP BY c.category_id, c.name, c.bucket_type, c.monthly_limit
        """)
        df = pd.read_sql(cat_sql, conn, params={"year": cur_year, "month": cur_month})

        user_sql = text("""
            SELECT u.name, COALESCE(SUM(CAST(t.amount AS FLOAT)), 0) as total_spent
            FROM users u
            LEFT JOIN transactions t ON u.user_id = t.user_id
                 AND EXTRACT(YEAR FROM t.date) = :year 
                 AND EXTRACT(MONTH FROM t.date) = :month
            GROUP BY u.user_id, u.name
        """)
        df_users = pd.read_sql(user_sql, conn, params={"year": cur_year, "month": cur_month})

    total_budget = float(df['monthly_limit'].sum())
    total_spent = float(df['spent'].sum())
    breakdown = df[df['spent'] > 0][['name', 'spent']].to_dict('records')
    category_coverage = df[['category_id', 'name', 'monthly_limit', 'spent']].to_dict('records')
    user_spending = df_users.to_dict('records')

    tyrone_row = df[df['name'] == 'Tyrone Allowance']
    nkosi_row = df[df['name'] == 'Nkosilathi Allowance']
    tyrone_spent = float(tyrone_row['spent'].values[0]) if not tyrone_row.empty else 0.0
    nkosi_spent = float(nkosi_row['spent'].values[0]) if not nkosi_row.empty else 0.0

    return {
        "total_budget": total_budget,
        "total_spent": total_spent,
        "fridge_fund": total_budget - total_spent,
        "breakdown": breakdown,
        "category_coverage": category_coverage,
        "user_spending": user_spending,
        "tyrone_spent": tyrone_spent,
        "nkosilathi_spent": nkosi_spent
    }


@app.post("/api/log-expense")
def log_expense(expense: Expense):
    resolved_cat = CATEGORY_ALIAS_MAP.get(expense.category_name, expense.category_name)
    with engine.begin() as conn:
        u_res = conn.execute(text("SELECT user_id FROM users WHERE name = :u"), {"u": expense.user_name}).fetchone()
        if not u_res:
            raise HTTPException(status_code=400, detail="User not found")
        c_res = conn.execute(text("SELECT category_id FROM categories WHERE name = :c"), {"c": resolved_cat}).fetchone()
        if not c_res:
            raise HTTPException(status_code=400, detail="Category not found")

        conn.execute(text("""
            INSERT INTO transactions (amount, user_id, category_id, description)
            VALUES (:a, :u, :c, :d)
        """), {"a": expense.amount, "u": u_res[0], "c": c_res[0], "d": expense.description})

    return {"message": f"Successfully logged ${expense.amount}"}


@app.put("/api/categories/{category_id}")
def update_category_limit(category_id: int, payload: CategoryUpdate):
    with engine.begin() as conn:
        conn.execute(text("UPDATE categories SET monthly_limit = :l WHERE category_id = :cid"),
                     {"l": payload.monthly_limit, "cid": category_id})
    return {"message": "Category limit updated"}


@app.get("/api/transactions")
def get_recent_transactions():
    with engine.connect() as conn:
        query = text("""
            SELECT t.transaction_id, u.name as user_name, c.name as category_name, 
                   CAST(t.amount AS FLOAT) as amount, t.description 
            FROM transactions t
            JOIN users u ON t.user_id = u.user_id
            JOIN categories c ON t.category_id = c.category_id
            ORDER BY t.transaction_id DESC
            LIMIT 10
        """)
        df = pd.read_sql(query, conn)
    return df.to_dict('records')


@app.delete("/api/transactions/{transaction_id}")
def delete_transaction(transaction_id: int):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM transactions WHERE transaction_id = :tid"), {"tid": transaction_id})
    return {"message": "Transaction deleted"}