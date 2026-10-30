"""Integrity guards enforced by the database itself, not just application code."""
from django.db import migrations

FORWARD = """
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only (% not allowed)', TG_TABLE_NAME, TG_OP USING ERRCODE = 'restrict_violation';
END $$;

CREATE TRIGGER transactions_immutable BEFORE UPDATE OR DELETE ON transactions
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER ledger_entries_immutable BEFORE UPDATE OR DELETE ON ledger_entries
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER wallet_state_history_immutable BEFORE UPDATE OR DELETE ON wallet_state_history
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER risk_decisions_immutable BEFORE UPDATE OR DELETE ON risk_decisions
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TRIGGER transactions_no_truncate BEFORE TRUNCATE ON transactions
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER ledger_entries_no_truncate BEFORE TRUNCATE ON ledger_entries
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- At commit time, each transaction's debits must equal its credits.
CREATE FUNCTION assert_journal_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE diff bigint;
BEGIN
  SELECT COALESCE(SUM(CASE WHEN direction = 'debit' THEN amount ELSE -amount END), 0)
    INTO diff FROM ledger_entries WHERE transaction_id = NEW.transaction_id;
  IF diff <> 0 THEN
    RAISE EXCEPTION 'unbalanced journal % (debits - credits = %)', NEW.transaction_id, diff;
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER ledger_entries_balanced AFTER INSERT ON ledger_entries
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION assert_journal_balanced();
"""

REVERSE = """
DROP TRIGGER ledger_entries_balanced ON ledger_entries;
DROP FUNCTION assert_journal_balanced();
DROP TRIGGER ledger_entries_no_truncate ON ledger_entries;
DROP TRIGGER transactions_no_truncate ON transactions;
DROP TRIGGER risk_decisions_immutable ON risk_decisions;
DROP TRIGGER wallet_state_history_immutable ON wallet_state_history;
DROP TRIGGER ledger_entries_immutable ON ledger_entries;
DROP TRIGGER transactions_immutable ON transactions;
DROP FUNCTION forbid_mutation();
"""


class Migration(migrations.Migration):
    dependencies = [("wallet", "0001_initial")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
