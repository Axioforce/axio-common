# import uuid
# from datetime import datetime
#
# from pydantic import BaseModel
# from typing import List
#
# from sqlalchemy import Column, String, ForeignKey, Float, DateTime
#
# from database import Base
# from logger_config import logger
# from utils.model_utils import current_time
#
#
# class MetricsResponse(BaseModel):
#     rmse: List[float] = []  # Root Mean Square Error for each axis
#     nrmse: List[float] = []  # Normalized RMSE
#     mae: List[float] = []  # Mean Absolute Error
#     mpe: List[float] = []  # Mean Percent Error
#     mape: List[float] = []  # Mean Absolute Percent Error
#
#     class Config:
#         from_attributes = True  # Enables SQLAlchemy model compatibility
#
#
# force_normalization_range: List[float] = [2000, 2000, 5000]  # Normalization range for force data [Newtons]
# moment_normalization_range: List[float] = [1000, 1000, 500]  # Normalization range for moment data [Newton-meters]
#
#
# class Metrics(Base):
#     __tablename__ = "metrics"
#     id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
#     run_id = Column(String, ForeignKey("runs.id"))
#     created_at = Column(DateTime, nullable=False, default=current_time)
#     updated_at = Column(DateTime, nullable=False, default=current_time, onupdate=current_time)
#     metric_type = Column(String)  # "train", "val", "test"
#     rmse_x = Column(Float)
#     rmse_y = Column(Float)
#     rmse_z = Column(Float)
#     nrmse_x = Column(Float)
#     nrmse_y = Column(Float)
#     nrmse_z = Column(Float)
#     mae_x = Column(Float)
#     mae_y = Column(Float)
#     mae_z = Column(Float)
#     mpe_x = Column(Float)
#     mpe_y = Column(Float)
#     mpe_z = Column(Float)
#     mape_x = Column(Float)
#     mape_y = Column(Float)
#     mape_z = Column(Float)
#
#     def calculate_nrmse(self, model_type, normalization_range):
#         """
#         Calculate the normalized RMSE for the metrics.
#         """
#         if self.rmse_x is not None:
#             self.nrmse_x = self.rmse_x / normalization_range[0] * 100.0
#             self.nrmse_y = self.rmse_y / normalization_range[1] * 100.0
#             self.nrmse_z = self.rmse_z / normalization_range[2] * 100.0
#
#     def change_normalization_range(self, norm_range: List[float], model_type: str):
#         """
#         Change the normalization range for the metrics.
#         """
#         if model_type == "force":
#             self.force_normalization_range = norm_range
#         elif model_type == "moment":
#             self.moment_normalization_range = norm_range
#
#     def summarize(self, normalization_range: List[float]):
#         """
#         Return a summary of the metrics.
#         """
#         return {
#             "rmse": [self.rmse_x, self.rmse_y, self.rmse_z],
#             "nrmse": [self.nrmse_x, self.nrmse_y, self.nrmse_z],
#             "mae": [self.mae_x, self.mae_y, self.mae_z],
#             "mpe": [self.mpe_x, self.mpe_y, self.mpe_z],
#             "mape": [self.mape_x, self.mape_y, self.mape_z],
#             "normalization": normalization_range
#         }
#
#     def to_dict(self):
#         """
#         Convert the metrics to a dictionary.
#         """
#         return {
#             "id": self.id,
#             "run_id": self.run_id,
#             "created_at": self.created_at,
#             "updated_at": self.updated_at,
#             "metric_type": self.metric_type,
#             "rmse_x": self.rmse_x,
#             "rmse_y": self.rmse_y,
#             "rmse_z": self.rmse_z,
#             "nrmse_x": self.nrmse_x,
#             "nrmse_y": self.nrmse_y,
#             "nrmse_z": self.nrmse_z,
#             "mae_x": self.mae_x,
#             "mae_y": self.mae_y,
#             "mae_z": self.mae_z,
#             "mpe_x": self.mpe_x,
#             "mpe_y": self.mpe_y,
#             "mpe_z": self.mpe_z,
#             "mape_x": self.mape_x,
#             "mape_y": self.mape_y,
#             "mape_z": self.mape_z
#         }
#
#     @classmethod
#     def from_dict(cls, data):
#         """
#         Create a Metrics object from a dictionary.
#         Filters out unexpected keys to prevent errors.
#         """
#         valid_keys = {column.name for column in cls.__table__.columns}
#         filtered_data = {key: value for key, value in data.items() if key in valid_keys}
#         if len(filtered_data) != len(data):
#             invalid_keys = set(data) - valid_keys
#             logger.warning(f"Invalid keys: {invalid_keys}")
#
#         return cls(**filtered_data)
#
#     def update_from_dict(self, data):
#         """
#         Update the Metrics object from a dictionary.
#         Ignores None values unless explicitly updating a field to None.
#         """
#         for key, value in data.items():
#             if key in self.to_dict() and value is not None:  # Only update if not None
#                 setattr(self, key, value)
#
#
# class MetricsRequest(BaseModel):
#     rmse: List[float] = []  # Root Mean Square Error for each axis
#     nrmse: List[float] = []  # Normalized RMSE
#     mae: List[float] = []  # Mean Absolute Error
#     mpe: List[float] = []  # Mean Percent Error
#     mape: List[float] = []  # Mean Absolute Percent Error
#     normalization: List[float] = []  # Normalization range for the metrics
#
#     class Config:
#         from_attributes = True  # Enables SQLAlchemy model compatibility
