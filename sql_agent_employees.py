# file: sql_agent_employees.py
import os
import sys
import psycopg2
import google.generativeai as genai
from typing import List, Dict, Any, Optional
import re
import json
import datetime

class SQLAgent:
    def __init__(self, database_url: str, google_api_key: str):
        self.database_url = database_url
        self.google_api_key = google_api_key
        self.conn = None
        self.cursor = None
        self.schema_info = None
        genai.configure(api_key=google_api_key)
        self.model = genai.GenerativeModel('gemini-2.5-flash-preview-04-17')
        self._connect_to_db()
        self._extract_schema_info()

    def _connect_to_db(self):
        try:
            self.conn = psycopg2.connect(self.database_url)
            self.cursor = self.conn.cursor()
            print("Successfully connected to the database.")
        except Exception as e:
            print(f"Error connecting to the database: {e}")
            sys.exit(1)

    def _is_safe_query(self, sql_query: str) -> bool:
        forbidden_keywords = [
            "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE", "REPLACE"
        ]
        sql_upper = sql_query.upper()
        if not sql_upper.strip().startswith("SELECT"):
            return False
        for keyword in forbidden_keywords:
            if re.search(rf'\b{keyword}\b', sql_upper):
                return False
        return True
    
    def _verify_natural_language_query(self, user_query: str) -> Dict[str, Any]:
        """
        Verifies if the input is a natural language query that is grammatically correct
        and suitable for SQL generation.
        
        Returns:
            Dict with verification result and feedback
        """
        prompt = f"""
        Your task is to verify if the following input is a proper natural language query 
        that can be converted to an SQL query. Assess if it's grammatically correct, 
        clear, and likely about database queries.
        
        USER INPUT:
        {user_query}
        
        Respond with JSON containing:
        1. "is_valid" (boolean): True if it's a valid natural language query, False otherwise
        2. "feedback" (string): Brief explanation of issues if invalid, or confirmation if valid
        3. "improved_query" (string): If the query has minor issues, provide an improved version. Otherwise, return the original query.
        
        Only respond with the JSON.
        """
        
        try:
            response = self.model.generate_content(prompt)
            result_text = response.text.strip()
            
            # Extract JSON from the response (in case model includes extra text)
            json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
            if json_match:
                result_text = json_match.group(0)
            
            result = json.loads(result_text)
            
            # Ensure all expected fields are present
            result.setdefault("is_valid", False)
            result.setdefault("feedback", "Failed to validate query format")
            result.setdefault("improved_query", user_query)
            
            return result
        except Exception as e:
            print(f"Error verifying natural language query: {e}")
            return {
                "is_valid": True,  # Default to True on validation failure to not block user
                "feedback": f"Validation service experienced an error: {str(e)}",
                "improved_query": user_query
            }

    def _extract_schema_info(self):
        try:
            self.cursor.execute("""
                SELECT table_schema, table_name
                FROM information_schema.tables 
                WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog', 'information_schema')
            """)
            tables = self.cursor.fetchall()
            schema_info = {}

            for schema, table in tables:
                full_table_name = f'"{schema}"."{table}"'

                self.cursor.execute(f"""
                    SELECT column_name, data_type, udt_name
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                """, (schema, table))
                columns = self.cursor.fetchall()

                column_defs = []
                for col in columns:
                    enum_values = []
                    if col[1] == 'USER-DEFINED':
                        self.cursor.execute("""
                            SELECT e.enumlabel
                            FROM pg_type t
                            JOIN pg_enum e ON t.oid = e.enumtypid
                            WHERE t.typname = %s
                        """, (col[2],))
                        enum_values = [r[0] for r in self.cursor.fetchall()]
                    column_defs.append({
                        "name": col[0],
                        "data_type": col[1],
                        "enum_values": enum_values
                    })

                self.cursor.execute(f"""
                    SELECT a.attname
                    FROM pg_index i
                    JOIN pg_attribute a ON a.attrelid = i.indrelid
                                       AND a.attnum = ANY(i.indkey)
                    WHERE i.indrelid = %s::regclass
                      AND i.indisprimary
                """, (f'"{schema}"."{table}"',))
                primary_keys = [pk[0] for pk in self.cursor.fetchall()]

                self.cursor.execute(f"""
                    SELECT kcu.column_name, ccu.table_schema, ccu.table_name, ccu.column_name
                    FROM information_schema.table_constraints AS tc
                    JOIN information_schema.key_column_usage AS kcu
                      ON tc.constraint_type = 'FOREIGN KEY' AND tc.constraint_name = kcu.constraint_name
                    JOIN information_schema.constraint_column_usage AS ccu
                      ON ccu.constraint_name = tc.constraint_name
                    WHERE tc.table_schema = %s AND tc.table_name = %s;
                """, (schema, table))
                foreign_keys = self.cursor.fetchall()
                foreign_keys = [
                    {
                        "column_name": fk[0],
                        "referenced_table": f"{fk[1]}.{fk[2]}",
                        "referenced_column": fk[3]
                    }
                    for fk in foreign_keys
                ]

                schema_info[f"{schema}.{table}"] = {
                    "columns": column_defs,
                    "primary_keys": primary_keys,
                    "foreign_keys": foreign_keys
                }

            self.schema_info = schema_info
            print("\n=== Database Schema Info ===")
        except Exception as e:
            print(f"Error extracting schema information: {e}")
            sys.exit(1)

    @staticmethod
    def default_serializer(obj):
        if isinstance(obj, (datetime.date, datetime.datetime, datetime.time)):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    def _generate_sql_query(self, user_query: str) -> str:
        schema_json = json.dumps(self.schema_info, indent=2, default=self.default_serializer)

        prompt = f"""
        You are an expert SQL query generator. Based on the following PostgreSQL database schema and the user's question,
        generate the most appropriate SQL query.

        Always use fully qualified names like \"schema\".\"Table\".\"column\".
        Never combine schema and table in one quoted string (e.g., avoid \"public.Table\").
        Quote schema, table, and column names individually to avoid PostgreSQL syntax errors.
        In the SQL query, use ILIKE operator for string matching with ignore case (e.g., ILIKE '%value%').

        DATABASE SCHEMA:
        {schema_json}

        USER QUESTION:
        {user_query}

        Generate ONLY the SQL query without any explanations. Make sure the query follows PostgreSQL syntax.
        """

        response = self.model.generate_content(prompt)
        sql_query = response.text.strip()
        print(f"Generated SQL query: {sql_query}")
        sql_query = re.sub(r'```sql|```', '', sql_query).strip()
        return sql_query

    def execute_query(self, sql_query: str) -> List[Dict[str, Any]]:
        try:
            self.cursor.execute(sql_query)
            rows = self.cursor.fetchall()
            column_names = [desc[0] for desc in self.cursor.description]
            return [dict(zip(column_names, row)) for row in rows]
        except Exception as e:
            self.conn.rollback()
            if 'does not exist' in str(e) and '.' in sql_query and '"' not in sql_query:
                print("Retrying with quoted identifiers...")
                retry_query = re.sub(r'(\b\w+\b)\.(\b\w+\b)', r'"\1"."\2"', sql_query)
                try:
                    self.cursor.execute(retry_query)
                    rows = self.cursor.fetchall()
                    column_names = [desc[0] for desc in self.cursor.description]
                    return [dict(zip(column_names, row)) for row in rows]
                except Exception as re2:
                    self.conn.rollback()
                    raise Exception(f"Original error: {e}\nRetry failed: {re2}")
            raise Exception(f"Error executing SQL query: {e}")

    def process_natural_language_query(self, user_query: str) -> Dict[str, Any]:
        try:
            # First, verify if the input is a proper natural language query
            verification_result = self._verify_natural_language_query(user_query)
            
            # Always use the improved query if available
            actual_query = verification_result.get("improved_query", user_query)
            
            # Only return error if the query is invalid AND no improved version is available
            if not verification_result["is_valid"] and actual_query == user_query:
                return {
                    "user_query": user_query,
                    "error": f"Invalid natural language query: {verification_result['feedback']}",
                    "status": "validation_failed"
                }
            
            # If using an improved version, inform the user
            if actual_query != user_query:
                print(f"Using improved query: {actual_query}")
            
            sql_query = self._generate_sql_query(actual_query)
            
            if not self._is_safe_query(sql_query):
                return {
                    "user_query": user_query,
                    "error": "Unsafe SQL detected. Only read-only SELECT queries are allowed.",
                    "status": "failed"
                }

            results = self.execute_query(sql_query)

            explanation_prompt = f"""
            Explain the following SQL query in simple terms, describing what it does:
            {sql_query}
            """
            explanation_response = self.model.generate_content(explanation_prompt)
            explanation = explanation_response.text.strip()

            return {
                "user_query": user_query,
                "processed_query": actual_query if actual_query != user_query else user_query,
                "sql_query": sql_query,
                "explanation": explanation,
                "results": results,
                "result_count": len(results)
            }
        except Exception as e:
            return {
                "user_query": user_query,
                "error": str(e),
                "status": "failed"
            }

    def close(self):
        if self.cursor:
            self.cursor.close()
        if self.conn:
            self.conn.close()
            print("Database connection closed.")


def main():
    database_url = os.environ.get("DATABASE_URL", "your_employees_db_url_here")
    google_api_key = os.environ.get("GOOGLE_API_KEY", "your_google_api_key_here")

    agent = SQLAgent(database_url, google_api_key)

    print("\n=== SQL Agent Ready ===")
    print("Type 'exit' to quit the application.")

    while True:
        user_input = input("\nEnter your query in natural language: ")

        if user_input.lower() in ['exit', 'quit', 'q']:
            agent.close()
            print("Thank you for using SQL Agent. Goodbye!")
            break

        try:
            result = agent.process_natural_language_query(user_input)

            if "error" in result:
                print(f"\nError: {result['error']}")
                # Automatically use the suggested query if available
                if "suggested_query" in result and result["suggested_query"] != user_input:
                    print(f"Using suggested query instead: {result['suggested_query']}")
                    # Reprocess with suggested query
                    result = agent.process_natural_language_query(result["suggested_query"])
            
            # Display results if no error or after using the suggested query
            if "error" not in result:
                if result['results']:
                    headers = result['results'][0].keys()
                    header_str = " | ".join(str(h) for h in headers)
                    print("\n" + header_str)
                    print("-" * len(header_str))
                    for row in result['results']:
                        print(" | ".join(str(val) for val in row.values()))
                    print(f"\nQuery Explanation: {result['explanation']}")
                else:
                    print("No results found.")

        except Exception as e:
            print(f"Error processing query: {e}")


if __name__ == "__main__":
    main()