# Microsoft SQL Server ODBC Driver Setup Guide

This guide walks you through installing and configuring the **Microsoft ODBC Driver for SQL Server** (`pyodbc`) for use with Antarkosh.

---

## 1. Overview

When connecting Antarkosh to **Microsoft SQL Server (`mssql`)**, Python uses `pyodbc`, which interfaces with the operating system's native ODBC driver.

By default, Antarkosh expects:
```ini
DB_ODBC_DRIVER="ODBC Driver 18 for SQL Server"
```
*(You can also use `"ODBC Driver 17 for SQL Server"` if that is what your environment has installed).*

---

## 2. Installation by Operating System

### Windows

1. Download the official installer:
   - **Microsoft ODBC Driver 18 for SQL Server (x64)**: [Download from Microsoft](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server)
2. Run the `.msi` installer and follow the on-screen wizard with default options.
3. Verify the installed driver in **PowerShell**:
   ```powershell
   Get-OdbcDriver -Name "ODBC Driver * for SQL Server"
   ```
   Or open **ODBC Data Sources (64-bit)** from the Start menu and check the **Drivers** tab.

---

### Linux (Ubuntu / Debian)

Microsoft provides official Debian/Ubuntu package repositories for the ODBC driver.

1. **Import the public repository GPG keys and add the Microsoft repository:**
   ```bash
   sudo su
   curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /etc/apt/trusted.gpg.d/microsoft.gpg
   
   # For Ubuntu 22.04:
   curl -sSL https://packages.microsoft.com/config/ubuntu/22.04/prod.list > /etc/apt/sources.list.d/mssql-release.list
   
   # (For Ubuntu 24.04, replace 22.04 with 24.04)
   exit
   ```

2. **Install `msodbcsql18` and `unixodbc-dev`:**
   ```bash
   sudo apt-get update
   sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev
   ```

3. **Verify registration:**
   ```bash
   cat /etc/odbcinst.ini
   ```
   You should see a section `[ODBC Driver 18 for SQL Server]`.

---

### macOS (Apple Silicon / Intel)

Install via Homebrew:

1. **Install `unixodbc` and Microsoft driver tap:**
   ```bash
   brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release
   brew update
   HOMEBREW_ACCEPT_EULA=Y brew install msodbcsql18 mssql-tools18 unixodbc
   ```

2. **Verify installation:**
   ```bash
   odbcinst -q -d
   ```

---

### Docker (Dockerfile Snippet)

If you are running Antarkosh in a container, add the following to your `Dockerfile`:

```dockerfile
# Install Microsoft ODBC Driver 18 for Debian/Ubuntu base images
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    unixodbc \
    unixodbc-dev \
 && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /etc/apt/trusted.gpg.d/microsoft.gpg \
 && curl -sSL https://packages.microsoft.com/config/debian/12/prod.list > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*
```

---

## 3. Verify in Python

Run this quick check using `uv run python`:

```python
import pyodbc
drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
print("Installed SQL Server drivers:", drivers)
```

If successful, you will see `['ODBC Driver 18 for SQL Server']` (or 17).

---

## 4. Configuring Antarkosh

### Option A: Via `.env` (Environment Variables)

In your root `.env` file:
```ini
DB_ENGINE=mssql
DB_HOST=127.0.0.1
DB_PORT=1433
DB_NAME=your_database_name
DB_READONLY_USER=readonly_user
DB_READONLY_PASSWORD=your_password
DB_ODBC_DRIVER="ODBC Driver 18 for SQL Server"
```

### Option B: Via Admin UI (Settings > Database Connection)

1. Open the Antarkosh UI in your browser.
2. Go to **Settings** -> **Database Connection**.
3. Select **Microsoft SQL Server** under *Database type*.
4. Enter Host, Port (`1433`), Database, Username, Password, and ODBC driver name.
5. Click **Test connection** to verify connectivity, then **Save**.

---

## 5. Troubleshooting

### 1. `[IM002] [Microsoft][ODBC Driver Manager] Data source name not found and no default driver specified`
- **Cause:** The string set in `DB_ODBC_DRIVER` does not match any registered driver on the system.
- **Fix:** Run `import pyodbc; print(pyodbc.drivers())` and copy the exact driver string into `DB_ODBC_DRIVER`.

### 2. `[08001] SSL Provider: [error:0A000086:SSL routines::certificate verify failed]`
- **Cause:** ODBC Driver 18 enforces encryption by default (`Encrypt=yes`).
- **Fix:** Ensure your SQL Server instance has a valid TLS certificate, or if connecting to a local development instance with a self-signed cert, configure `TrustServerCertificate=yes` in your connection parameters.

### 3. Architecture Mismatch (32-bit vs 64-bit)
- If Python is 64-bit (standard), you **must** install the 64-bit version of the ODBC Driver. 32-bit drivers cannot be loaded by a 64-bit Python process.
