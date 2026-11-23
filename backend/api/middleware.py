class RequireLoginMiddleware:
    # MCP authenticates separately with its required Bearer token.
    public_paths = {"/api/health/", "/api/auth/login/", "/api/auth/me/", "/api/mcp/"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (request.path_info.startswith("/api/")
                and request.path_info not in self.public_paths
                and not request.user.is_authenticated):
            from django.http import JsonResponse

            return JsonResponse({"error": "请先登录", "needLogin": True}, status=401)
        return self.get_response(request)


class CorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "OPTIONS":
            from django.http import HttpResponse

            response = HttpResponse()
        else:
            response = self.get_response(request)

        response["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
        response["Access-Control-Allow-Methods"] = "GET,POST,PATCH,OPTIONS"
        response["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        response["Access-Control-Allow-Credentials"] = "true"
        return response
