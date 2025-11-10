from fastapi import FastAPI
from routers.stocknames import router  # replace with actual filename

app = FastAPI()
app.include_router(router)
