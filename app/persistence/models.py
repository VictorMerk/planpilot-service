from .db import db


class FastDownwardRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    hash_value = db.Column(db.String(64), unique=True, nullable=False)
    domain_file_path = db.Column(db.String(512), nullable=False)
    problem_file_path = db.Column(db.String(512), nullable=False)
    sas_file_path = db.Column(db.String(512))
    plan_file_path = db.Column(db.String(512))
