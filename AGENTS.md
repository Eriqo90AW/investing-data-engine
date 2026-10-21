# API documentation maintenance

The Swagger page at `/docs` is the endpoint documentation and manual testing interface. FastAPI's `/openapi.json` is its source. Keep documentation beside route declarations and Pydantic schemas; do not add a separate frontend or duplicate endpoint catalog.

Whenever development adds or changes a route, parameter, request body, response, status code, header, error, or extraction capability, update its OpenAPI descriptions and examples in the same change. Include successful and relevant error responses. Document binary responses as files and conditional 304 responses as empty bodies.

Keep the Swagger introduction and README workflow consistent with what the engine currently supports. Clearly label examples as illustrative; never present sample financial values as retrieved or verified results. Update the extraction limitation when verified adapters become available.

Keep focused tests for endpoint coverage, request/response examples, and status codes current. Run `python -m pytest` with the project's test dependencies installed before completing API documentation changes. Manually check affected Swagger controls when browser testing is available, and report any verification that could not run.

Requests run only after the user clicks Execute. Keep results within the current page visit; do not add saved request history or automatic extraction/polling without a separate request.
