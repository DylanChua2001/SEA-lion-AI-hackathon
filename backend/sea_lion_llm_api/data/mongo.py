from pymongo import MongoClient

from sea_lion_llm_api.config import settings

client = MongoClient(settings.MONGODB_URI)
db = client[settings.DB_NAME]