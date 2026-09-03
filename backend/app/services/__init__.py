"""Services package for domain orchestration and application execution.

Do not re-export OrderManager here: `app.oms.ibkr_adapter` imports
`app.services.account_margin`, and OrderManager imports the OMS stack.
A package-level re-export would create a circular import on adapter load.
"""
