from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Injects strict HTTP security headers to protect against common web vulnerabilities
    like XSS, Clickjacking, and content sniffing.
    """
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        
        # Prevent browsers from MIME-sniffing a response away from the declared content-type
        response.headers["X-Content-Type-Options"] = "nosniff"
        
        # Prevent Clickjacking by restricting framing
        response.headers["X-Frame-Options"] = "DENY"
        
        # Enable XSS filtering in legacy browsers
        response.headers["X-XSS-Protection"] = "1; mode=block"
        
        # Strict Transport Security (HSTS)
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        
        # Content Security Policy (restrict sources of executable scripts)
        # Note: 'unsafe-inline' is often needed for swagger docs, but in production we'd restrict further.
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline';"
        
        # Prevent the browser from sending the Referer header
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        
        return response
