# Candidate kit

# Employee Attendance API

## 1. Project Overview

The Employee Attendance API is a backend application built using **Python, FastAPI, and MongoDB**. It provides API endpoints for managing employee attendance records and retrieving attendance-related information.

FastAPI handles incoming HTTP requests, while MongoDB stores the application data.

## 2. Technologies Used

- **Python:** Main programming language.
- **FastAPI:** Framework used to build REST APIs.
- **Uvicorn:** Server used to run the FastAPI application.
- **MongoDB:** Database used to store attendance data.
- **PyMongo:** Python library used to connect to MongoDB.
- **Python dotenv:** Used to load configuration values from a `.env` file.

## 3. Installation

### Step 1: Create a virtual environment

```bash
python -m venv .venv
```

### Step 2: Activate the environment (Windows PowerShell)

```powershell
.\.venv\Scripts\Activate.ps1
```

### Step 3: Install the required packages

```bash
python -m pip install fastapi "uvicorn[standard]" pymongo python-dotenv
```

### Step 4: Configure MongoDB

Ensure MongoDB is running and configure the database connection in your `.env` file.

Example:

```env
MONGO_URI=mongodb://localhost:abcd
MONGO_DB=employee_attendance
```

## 4. How the Application Works

1. A client sends an HTTP request to an API endpoint.
2. FastAPI receives the request and validates the input.
3. The application processes the request and communicates with MongoDB when required.
4. MongoDB stores or retrieves the requested data.
5. FastAPI returns an HTTP response, usually in JSON format.

**Request flow:**

Client → FastAPI → Application Logic → MongoDB → JSON Response

## 5. Run the Application

Open a terminal in the project directory and activate the virtual environment.

Run:

```bash
uvicorn app.main:app --reload
```

The application will run locally at:

http://127.0.0.1:8000

The `--reload` option automatically restarts the server when the source code changes.

## 6. Verify the API

### Interactive API documentation

Open the following URL in your browser:

http://127.0.0.1:8000/docs#/

The Swagger UI documentation allows you to:

- View available API endpoints.
- Check request parameters and response formats.
- Send test requests using **Try it out**.
- Execute endpoints and inspect their responses and HTTP status codes.

Alternative documentation is available at:

http://127.0.0.1:8000/redoc

**Note:** Opening `http://127.0.0.1:8000/` may show `404 Not Found` if a root endpoint has not been defined. This does not necessarily mean the server is broken.

## 7. Troubleshooting

- **404 Not Found:** Check whether the requested URL matches a defined endpoint.
- **Could not import module:** Run the command from the project root and verify that `app/main.py` exists.
- **MongoDB connection error:** Check that MongoDB is running and the URI in `.env` is correct.
- **Package not found:** Activate the virtual environment and install the required package.

## 8. Project Status

The development environment and MongoDB connection have been configured. API functionality should be verified through the Swagger documentation and tested against the requirements of the assignment.

