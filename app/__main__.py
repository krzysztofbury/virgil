import uvicorn

from app.config import HOST, IS_PROD, PORT

uvicorn.run(
    "app.main:app",
    host=HOST,
    port=PORT,
    reload=not IS_PROD,
)
