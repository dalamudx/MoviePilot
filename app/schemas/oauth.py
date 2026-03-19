from pydantic import BaseModel
from typing import Optional


class OAuthStateData(BaseModel):
    """OAuth state 缓存数据"""
    code_verifier: Optional[str] = None  # PKCE
    redirect_path: Optional[str] = "/"  # 登录后重定向路径
    timestamp: float


class OAuthUserInfo(BaseModel):
    """OAuth 用户信息"""
    sub: str  # subject, 唯一标识
    preferred_username: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    picture: Optional[str] = None
