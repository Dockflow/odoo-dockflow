-- A copied database must not send to, or poll from, the production Dockflow account.
DELETE FROM ir_config_parameter WHERE key IN ('dockflow_tracking.token', 'dockflow_tracking.polling_key');
UPDATE ir_config_parameter SET value = 'False' WHERE key = 'dockflow_tracking.opt_in';
