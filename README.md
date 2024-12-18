# HAR to OpenAPI Converter

This project provides a utility for converting HTTP Archive (HAR) files into OpenAPI 3.0 specifications. It simplifies the process of generating OpenAPI schemas from captured HTTP requests and responses, allowing easier integration into tools like Swagger UI or API testing platforms.

## Features
- **URL Filtering:** Filter requests by domain or URL pattern.
- **Cookie Filtering:** Retain specific cookies in requests.
- **Header Ignoring:** Exclude specified headers from requests.
- **Automatic OpenAPI Schema Generation:** Create OpenAPI files grouped by base URL.

## Requirements
- Python 3.8+
- Dependencies listed in `requirements.txt`

## Installation

1. Clone the repository:
   ```bash
   git clone <repository_url>
   cd har2openapi
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

The project consists of two main components:

1. **`har2openapi` Class:** Contains the core logic for converting HAR files to OpenAPI schemas.
2. **`main.py`:** A script that provides an interactive way to use the converter.

### Running the Script

To generate OpenAPI files interactively, run:
```bash
python main.py
```

You will be prompted to provide the following inputs:
- **Path to HAR file:** The full path to the HAR file you want to convert.
- **URL Filter:** The domain or URL pattern to filter requests (e.g., `example.com`).
- **Cookies to Keep:** A comma-separated list of cookie names to include (e.g., `sid,auth_token`).
- **Headers to Ignore:** A comma-separated list of header names to exclude (e.g., `User-Agent,Authorization`).

The script will process the HAR file and generate one or more OpenAPI files in the current directory.

### Example Input
```
Enter the path to the HAR file:
> /path/to/file.har

Enter a regular expression to filter URLs (or leave empty):
> example.com

Enter a list of cookies to filter (comma-separated, or leave empty):
> sid,auth_token

Enter a list of headers to ignore (comma-separated, or leave empty):
> User-Agent,Authorization

```

### Example Output
The script generates OpenAPI YAML files in the following format:
```
openapi_example_com.yaml
openapi_another_example_com.yaml
```

### Programmatic Use

You can use the `har2openapi` class directly in your Python scripts:

```python
from har2openapi import har2openapi

har_converter = har2openapi(
    filename='/path/to/file.har',
    url_filter='example.com',
    cookie_filter=['sid', 'auth_token'],
    ignore_headers=['User-Agent', 'Authorization']
)

har_converter.create_openapi()
```

## Output
Each OpenAPI file includes:
- **Paths:** Extracted from the HAR file's request URLs.
- **Methods:** HTTP methods (GET, POST, etc.) used in requests.
- **Headers and Parameters:** Request headers, query parameters, and cookies.
- **Request and Response Bodies:** Automatically formatted based on HAR data.

## Customization

### URL Filtering
You can use simple domain names (e.g., `example.com`) or full regular expressions (e.g., `^https://api\.example\.com/.*`).

### Cookie Filtering
Specify which cookies to include using their names. For example, to retain `sid` and `auth_token`, provide:
```plaintext
sid,auth_token
```

### Ignoring Headers
List the headers you want to exclude from the OpenAPI schema. For example, to ignore `User-Agent` and `Authorization`:
```plaintext
User-Agent,Authorization
```

