from slowapi import Limiter
from slowapi.util import get_remote_address

# Initialize the global rate limiter using the client's IP address.
# In a real distributed system, you would back this with Redis via slowapi.extensions.redis
limiter = Limiter(key_func=get_remote_address)

def get_real_ip(request):
    """
    Helper to extract the real IP behind load balancers or proxies (e.g. Nginx/AWS ALB)
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"

# We update the default key func to handle proxies correctly in production
limiter.key_func = get_real_ip
