"""Fixture procedures for the Phase 5 tests: one per dialect plus six messy real-world-style T-SQL ones."""

# One realistic procedure per dialect (engine key -> text)
SAMPLES = {
    "mssql": r"""
CREATE PROCEDURE dbo.usp_PO_Summary @from DATE, @status VARCHAR(10) = 'OPEN'
AS
BEGIN
  SET NOCOUNT ON;
  CREATE TABLE #tmp (po_id INT, total DECIMAL(18,2));
  INSERT INTO #tmp (po_id, total)
  SELECT h.po_id, SUM(d.qty * d.rate)
  FROM dbo.po_head h
  INNER JOIN dbo.po_detail d ON d.po_id = h.po_id
  LEFT JOIN dbo.party p ON p.party_id = h.party_id
  WHERE h.status = @status AND h.po_type IN ('R','S') AND h.created_on >= @from
  GROUP BY h.po_id;
  IF @status = 'OPEN'
  BEGIN
     UPDATE dbo.po_head SET flag = 1 WHERE po_id IN (SELECT po_id FROM #tmp);
  END
  SELECT t.po_id, CASE h.po_type WHEN 'R' THEN 'Regular' WHEN 'S' THEN 'Special' END AS kind
  FROM #tmp t JOIN dbo.po_head h ON h.po_id = t.po_id;
  EXEC('SELECT * FROM dbo.audit WHERE x = ' + @status);
  DROP TABLE #tmp;
END
""",
    "postgresql": r"""
CREATE OR REPLACE FUNCTION sales.order_report(p_from date) RETURNS TABLE(id int, total numeric) LANGUAGE plpgsql AS $$
DECLARE v_cnt int;
BEGIN
  CREATE TEMP TABLE tmp_o AS SELECT o.id, o.total FROM sales.orders o JOIN sales.customers c ON c.id = o.customer_id WHERE o.status = 'paid' AND o.created >= p_from;
  SELECT count(*) INTO v_cnt FROM tmp_o;
  IF v_cnt > 0 THEN
    UPDATE sales.orders SET reported = true WHERE id IN (SELECT id FROM tmp_o);
  END IF;
  RETURN QUERY SELECT t.id, t.total FROM tmp_o t;
END $$
""",
    "mysql": r"""
CREATE PROCEDURE close_order(IN o INT)
BEGIN
  DECLARE v_total DECIMAL(10,2);
  SELECT SUM(l.qty*l.price) INTO v_total FROM order_lines l JOIN orders od ON od.id = l.order_id WHERE od.id = o AND l.kind IN ('A','B');
  UPDATE orders SET total = v_total, status = 'CLOSED' WHERE id = o;
  INSERT INTO order_log (order_id, note) VALUES (o, 'closed');
END
""",
    "oracle": r"""
CREATE OR REPLACE PROCEDURE hr.raise_sal(p_dept NUMBER) IS
  CURSOR c IS SELECT e.employee_id, e.salary FROM hr.employees e JOIN hr.departments d ON d.department_id = e.department_id WHERE d.department_id = p_dept AND e.job_id IN ('SA_REP','IT_PROG');
BEGIN
  FOR r IN c LOOP
    UPDATE hr.employees SET salary = salary * 1.1 WHERE employee_id = r.employee_id;
  END LOOP;
  INSERT INTO hr.audit_log (msg) VALUES ('done');
  COMMIT;
END raise_sal;
""",
}

# Six messy T-SQL procedures: no semicolons, NOLOCK, CTEs, MERGE, UPDATE..FROM, cursors, dynamic SQL, APPLY
HARD_TSQL = {
    "nosemi_nolock": r"""
CREATE PROCEDURE [dbo].[usp_StockReport] @wh INT, @asof DATETIME = NULL
AS
SET NOCOUNT ON
DECLARE @t TABLE (item_id INT, qty DECIMAL(18,3))
INSERT INTO @t (item_id, qty)
SELECT TOP 100 s.item_id, SUM(s.qty)
FROM [dbo].[stock] s WITH (NOLOCK)
INNER JOIN [dbo].[item_master] im WITH (NOLOCK) ON im.item_id = s.item_id
WHERE s.wh_id = @wh AND ISNULL(s.status,'A') = 'A' AND im.item_type IN ('FG','RM')
GROUP BY s.item_id
SELECT t.item_id, im.item_name FROM @t t JOIN dbo.item_master im ON im.item_id = t.item_id
ORDER BY t.qty DESC
""",
    "cte_union": r"""
CREATE PROCEDURE dbo.usp_Sales @from DATE AS
BEGIN
  ;WITH inv AS (
     SELECT i.inv_id, i.party_id, i.total FROM dbo.sales_invoice i WHERE i.inv_date >= @from AND i.doc_type = 'INV'
  ), ret AS (
     SELECT r.inv_id, r.party_id, -r.total AS total FROM dbo.sales_return r WHERE r.doc_type = 'SRT'
  )
  SELECT p.party_name, x.total
  FROM (SELECT * FROM inv UNION ALL SELECT * FROM ret) x
  JOIN dbo.party p ON p.party_id = x.party_id
END
""",
    "update_from_join": r"""
CREATE PROC dbo.usp_Fix AS
BEGIN
  UPDATE po
  SET po.status = 'CLOSED'
  FROM dbo.po_head po
  INNER JOIN dbo.po_detail pd ON pd.po_id = po.po_id
  WHERE pd.pending_qty = 0 AND po.status <> 'CLOSED'

  DELETE d FROM dbo.grn_detail d JOIN dbo.grn_head h ON h.grn_id = d.grn_id WHERE h.status = 'CANCELLED'
END
""",
    "merge_trycatch": r"""
CREATE PROCEDURE dbo.usp_Sync AS
BEGIN
  BEGIN TRY
    BEGIN TRAN
    MERGE dbo.item_price AS tgt
    USING dbo.item_price_stage AS src ON tgt.item_id = src.item_id AND tgt.price_list = src.price_list
    WHEN MATCHED THEN UPDATE SET tgt.price = src.price
    WHEN NOT MATCHED THEN INSERT (item_id, price_list, price) VALUES (src.item_id, src.price_list, src.price);
    COMMIT TRAN
  END TRY
  BEGIN CATCH
    ROLLBACK TRAN
    INSERT INTO dbo.error_log (msg) VALUES (ERROR_MESSAGE())
  END CATCH
END
""",
    "dynamic_and_cursor": r"""
CREATE PROCEDURE dbo.usp_Dyn @tbl SYSNAME AS
BEGIN
  DECLARE @sql NVARCHAR(MAX) = N'SELECT * FROM ' + QUOTENAME(@tbl) + N' WHERE x = 1'
  EXEC sp_executesql @sql
  DECLARE c CURSOR FOR SELECT e.emp_id FROM dbo.employee e WHERE e.active = 'Y'
  OPEN c
  SELECT e.emp_id, e.dept_id INTO #work FROM dbo.employee e JOIN dbo.department d ON d.dept_id = e.dept_id WHERE d.dept_type = 'PROD'
  SELECT w.emp_id FROM #work w
END
""",
    "outer_apply_scalar_sub": r"""
CREATE PROCEDURE dbo.usp_Cust AS
SELECT c.cust_id, c.cust_name,
       (SELECT MAX(o.order_date) FROM dbo.sales_order o WHERE o.cust_id = c.cust_id) AS last_order,
       oa.cnt
FROM dbo.customer c
OUTER APPLY (SELECT COUNT(*) AS cnt FROM dbo.sales_order o2 WHERE o2.cust_id = c.cust_id AND o2.status = 'OPEN') oa
WHERE c.cust_type = 'RETAIL'
""",
}
