{
    "name": "Project AI Conductor",
    "version": "20.0.1.7.0",
    "summary": "Proactive project risk assistant for Odoo",
    "category": "Services/Project",
    "license": "LGPL-3",
    "depends": ["project", "mail_tracking", "web"],
    "data": ["views/risk_summary.xml", "views/project_morning.xml", "data/morning_cron.xml", "views/risk_detail.xml"],
    "installable": True,
}
