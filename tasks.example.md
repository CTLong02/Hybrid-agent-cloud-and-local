# Task: T001 — Create Student model
- depends_on:
- tags: model, database
- complexity: low
- files: src/models/student.py, src/models/__init__.py
- acceptance: Student model with id, name, email, dob fields and SQLAlchemy mapping

Create a SQLAlchemy model `Student` matching the existing pattern (see Teacher
model). Include `__repr__` and `to_dict()` helpers. Add an Alembic migration.

# Task: T002 — Create Student schema
- depends_on: T001
- tags: schema, validation
- complexity: low
- files: src/schemas/student.py
- acceptance: Pydantic StudentCreate, StudentUpdate, StudentRead schemas with validation

Pydantic v2 schemas. Email must be validated. DOB must be in the past.

# Task: T003 — Create Student API
- depends_on: T001, T002
- tags: api, crud
- complexity: medium
- files: src/api/students.py, src/api/__init__.py
- acceptance: |
    POST /students returns 201 with the created student
    GET /students/{id} returns 200 or 404
    GET /students returns 200 with a paginated list
    PUT /students/{id} returns 200
    DELETE /students/{id} returns 204

Standard FastAPI router. Use the existing `get_db` dependency. Follow the
error-envelope convention used by the Teacher API.

# Task: T004 — Wire Student API into app
- depends_on: T003
- tags: api, wiring
- complexity: low
- files: src/main.py
- acceptance: Student router included; /docs lists the endpoints

# Task: T005 — Add Student API tests
- depends_on: T003
- tags: test, api
- complexity: medium
- files: tests/api/test_students.py
- acceptance: All five endpoints have happy-path + edge-case tests; pytest passes

Cover: validation failures, 404 cases, pagination, and concurrent updates.

# Task: T006 — Add audit logging to Student endpoints
- depends_on: T003
- tags: api, security
- complexity: medium
- files: src/api/students.py, src/audit/__init__.py
- acceptance: Every write op emits an audit log with user id, action, before/after

This is tagged `security` so routing will send it to Claude regardless of
complexity.
