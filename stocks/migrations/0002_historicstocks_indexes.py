from django.db import migrations

# historicstocks is unmanaged (managed=False), so its indexes are created with raw SQL.
# They speed up the per-symbol / per-token window and range queries in stocks.analysis.screeners.


class Migration(migrations.Migration):

    dependencies = [
        ("stocks", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql="CREATE INDEX IF NOT EXISTS historicstocks_symbol_date_idx ON historicstocks (symbol, date);",
            reverse_sql="DROP INDEX IF EXISTS historicstocks_symbol_date_idx;",
        ),
        migrations.RunSQL(
            sql="CREATE INDEX IF NOT EXISTS historicstocks_symboltoken_date_idx ON historicstocks (symboltoken, date);",
            reverse_sql="DROP INDEX IF EXISTS historicstocks_symboltoken_date_idx;",
        ),
        migrations.RunSQL(
            sql="CREATE INDEX IF NOT EXISTS historicstocks_date_idx ON historicstocks (date);",
            reverse_sql="DROP INDEX IF EXISTS historicstocks_date_idx;",
        ),
        migrations.RunSQL(sql="ANALYZE historicstocks;", reverse_sql=migrations.RunSQL.noop),
    ]
