import secrets
import hashlib
import base64
import time
import json
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query
import httpx

from app import schemas
from app.core.config import settings
from app.core.security import create_access_token, get_password_hash
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import UserOper
from app.helper.sites import SitesHelper
from app.log import logger
from app.schemas.oauth import OAuthStateData
from app.schemas.types import SystemConfigKey

router = APIRouter()

# OAuth state 缓存 (简单内存缓存, 10 分钟过期)
# 生产环境应使用 Redis
OAUTH_STATE_CACHE: dict[str, OAuthStateData] = {}


def cleanup_expired_states():
    """清理过期的 state"""
    current_time = time.time()
    expired_keys = [
        key for key, data in OAUTH_STATE_CACHE.items()
        if current_time - data.timestamp > 600  # 10 分钟
    ]
    for key in expired_keys:
        OAUTH_STATE_CACHE.pop(key, None)


def generate_pkce_pair() -> tuple[str, str]:
    """生成 PKCE code_verifier 和 code_challenge"""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8').rstrip('=')
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode('utf-8')).digest()
    ).decode('utf-8').rstrip('=')
    return code_verifier, code_challenge



def get_auth_value(key: str) -> Any:
    """获取认证配置(DB)"""
    conf = SystemConfigOper().get(SystemConfigKey.SystemAuth) or {}
    return conf.get(key)


def validate_oauth_config():
    """验证 OAuth 配置是否完整"""
    if not get_auth_value("OAUTH_ENABLE"):
        raise HTTPException(status_code=400, detail="OAuth 认证未启用")
    
    required_fields = {
        "OAUTH_CLIENT_ID": get_auth_value("OAUTH_CLIENT_ID"),
        "OAUTH_CLIENT_SECRET": get_auth_value("OAUTH_CLIENT_SECRET"),
        "OAUTH_AUTHORIZATION_ENDPOINT": get_auth_value("OAUTH_AUTHORIZATION_ENDPOINT"),
        "OAUTH_TOKEN_ENDPOINT": get_auth_value("OAUTH_TOKEN_ENDPOINT"),
    }
    
    missing = [field for field, value in required_fields.items() if not value]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"OAuth 配置不完整,缺少: {', '.join(missing)}"
        )


@router.get("/authorize", summary="启动 OAuth 授权流程")
def oauth_authorize(
    redirect_path: str = Query("/", description="登录后重定向路径")
) -> dict[str, str]:
    """
    启动 OAuth 授权流程
    返回授权 URL,前端需重定向到此 URL
    """
    validate_oauth_config()
    cleanup_expired_states()
    
    # 生成 state
    state = secrets.token_urlsafe(32)
    
    # 生成 PKCE
    code_verifier, code_challenge = None, None
    if get_auth_value("OAUTH_USE_PKCE"):
        code_verifier, code_challenge = generate_pkce_pair()
    
    # 缓存 state 数据
    OAUTH_STATE_CACHE[state] = OAuthStateData(
        code_verifier=code_verifier,
        redirect_path=redirect_path,
        timestamp=time.time()
    )
    
    # 构建授权 URL
    redirect_uri = get_auth_value("OAUTH_REDIRECT_URI") or f"{settings.APP_DOMAIN}/oauth/callback"
    params = {
        "client_id": get_auth_value("OAUTH_CLIENT_ID"),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": get_auth_value("OAUTH_SCOPE"),
        "state": state,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    
    auth_url = f"{get_auth_value('OAUTH_AUTHORIZATION_ENDPOINT')}?{urlencode(params)}"
    return {"authorization_url": auth_url}


@router.post("/callback", summary="OAuth 回调处理", response_model=schemas.Token)
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
) -> Any:
    """
    OAuth 回调处理
    交换 authorization code 为 access token,获取用户信息,生成 JWT
    """
    validate_oauth_config()
    
    # 验证 state
    state_data = OAUTH_STATE_CACHE.pop(state, None)
    if not state_data:
        raise HTTPException(status_code=400, detail="Invalid state 或 state 已过期")
    
    # 检查 state 是否过期(10分钟)
    if time.time() - state_data.timestamp > 600:
        raise HTTPException(status_code=400, detail="State 已过期")
    
    redirect_uri = f"{settings.APP_DOMAIN}/oauth/callback"
    
    # 交换 code 为 token
    # 注意: OAuth 回调通常不使用代理以确保连接稳定性
    async with httpx.AsyncClient(timeout=30.0) as client:
        token_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": get_auth_value("OAUTH_CLIENT_ID"),
            "client_secret": get_auth_value("OAUTH_CLIENT_SECRET"),
        }
        if state_data.code_verifier:
            token_data["code_verifier"] = state_data.code_verifier
        
        try:
            token_response = await client.post(
                get_auth_value("OAUTH_TOKEN_ENDPOINT"), 
                data=token_data
            )
            token_response.raise_for_status()
            tokens = token_response.json()
        except Exception as e:
            logger.error(f"OAuth token 交换失败: {e}")
            raise HTTPException(status_code=500, detail=f"Token 交换失败: {str(e)}")
        
        # 获取用户信息
        access_token = tokens.get("access_token")
        if not access_token:
            raise HTTPException(status_code=500, detail="未获取到 access_token")
        
        # 对于 OIDC,优先从 id_token 中解析用户信息
        userinfo = None
        if get_auth_value("OAUTH_PROVIDER_TYPE") == "oidc" and "id_token" in tokens:
            # 解析 id_token (JWT)
            # 注意: 生产环境应验证签名
            import jwt
            id_token = tokens["id_token"]
            try:
                userinfo = jwt.decode(id_token, options={"verify_signature": False})
                logger.info("从 id_token 中获取用户信息")
            except Exception as e:
                logger.warning(f"id_token 解析失败: {e}")
        
        # 如果没有从 id_token 获取,或者想要同步头像但 id_token 中没有头像,则调用 userinfo 端点
        # 注意: ID Token 为了减小体积通常不包含 picture 字段
        need_fetch_userinfo = not userinfo
        if userinfo and get_auth_value("OAUTH_SYNC_AVATAR") and not userinfo.get("picture"):
            need_fetch_userinfo = True
            
        userinfo_endpoint = get_auth_value("OAUTH_USERINFO_ENDPOINT")
        if need_fetch_userinfo and userinfo_endpoint:
            try:
                userinfo_response = await client.get(
                    userinfo_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"}
                )
                userinfo_response.raise_for_status()
                fetched_info = userinfo_response.json()
                logger.info("从 userinfo 端点获取用户信息")
                
                # 合并信息 (以 endpoint 为准)
                if userinfo:
                    userinfo.update(fetched_info)
                else:
                    userinfo = fetched_info
            except Exception as e:
                logger.error(f"获取用户信息失败: {e}")
                if not userinfo:
                    raise HTTPException(status_code=500, detail=f"获取用户信息失败: {str(e)}")
    
    if not userinfo:
        raise HTTPException(status_code=500, detail="未能获取用户信息")
    
    logger.debug(f"OAuth 用户信息: {userinfo}")
    
    # 提取用户名
    username_field = get_auth_value("OAUTH_USERNAME_FIELD")
    username = (
        userinfo.get(username_field) or
        userinfo.get("preferred_username") or
        userinfo.get("email") or
        userinfo.get("sub")
    )
    if not username:
        raise HTTPException(
            status_code=500,
            detail=f"无法从用户信息中提取用户名,字段: {username_field}"
        )
    
    # 提取邮箱和头像
    email = userinfo.get("email")
    # 优先从自定义字段获取头像,默认为 picture
    avatar_field = get_auth_value("OAUTH_AVATAR_FIELD") or "picture"
    avatar_url = userinfo.get(avatar_field)
    
    # 兼容性: 如果自定义字段没取到,且自定义字段不是 picture,则尝试取 picture
    if not avatar_url and avatar_field != "picture":
        avatar_url = userinfo.get("picture")
        
    avatar = None
    
    # 如果启用头像同步且有头像URL,尝试下载处理
    if get_auth_value("OAUTH_SYNC_AVATAR") and avatar_url:
        try:
            logger.info(f"开始同步 OAuth 头像: {avatar_url}")
            # 使用 httpx 下载图片 (30秒超时,最大800KB)
            async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
                resp = await client.get(avatar_url)
                if resp.status_code == 200:
                    content = resp.content
                    if len(content) > 800 * 1024:
                        logger.warning(f"OAuth 头像超过 800KB 限制,放弃同步: {len(content)/1024:.2f}KB")
                    else:
                        content_type = resp.headers.get("content-type", "image/jpeg")
                        b64_img = base64.b64encode(content).decode('utf-8')
                        avatar = f"data:{content_type};base64,{b64_img}"
                        logger.info(f"OAuth 头像处理成功,大小: {len(content)/1024:.2f}KB")
                else:
                    logger.warning(f"下载 OAuth 头像失败: HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"同步 OAuth 头像失败: {e}")

    # 查找或创建用户
    useroper = UserOper()
    user = useroper.get_by_name(username)
    
    is_new_user = False
    if not user:
        if not get_auth_value("OAUTH_AUTO_CREATE_USER"):
            raise HTTPException(
                status_code=403,
                detail=f"用户 {username} 不存在且不允许自动创建"
            )
        
        is_new_user = True
        
        # 解析默认权限
        default_permissions = {}
        new_user_perms = get_auth_value("OAUTH_NEW_USER_PERMISSIONS")
        if new_user_perms:
            try:
                # 支持逗号分隔的权限配置
                perms = [p.strip() for p in new_user_perms.split(',')]
                for p in perms:
                    if p:
                        default_permissions[p] = True
                logger.info(f"OAuth 新用户默认权限: {default_permissions}")
            except Exception as e:
                logger.warning(f"解析 OAUTH_NEW_USER_PERMISSIONS 失败: {e},将使用空权限")
        
        # 创建新用户 (注意: useroper.add() 没有返回值)
        try:
            useroper.add(
                name=username,
                email=email if get_auth_value("OAUTH_SYNC_EMAIL") else None,
                is_active=True,
                is_superuser=False,  # OAuth 用户默认为普通用户
                hashed_password=get_password_hash(secrets.token_urlsafe(32)),  # 随机密码
                avatar=avatar if avatar else avatar_url,  # 优先使用 Base64,否则存 URL
                permissions=default_permissions,  # 设置默认权限
            )
            logger.info(f"通过 OAuth2 自动创建用户: {username}, 邮箱: {email}, 权限: {default_permissions}")
            
            # 重新查询用户
            user = useroper.get_by_name(username)
            if not user:
                raise HTTPException(
                    status_code=500,
                    detail=f"创建用户 {username} 后无法查询到该用户"
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"创建用户 {username} 失败: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"创建用户失败: {str(e)}"
            )
    else:
        # 已存在的用户,同步信息 (邮箱和头像)
        update_data = {}
        
        # 同步邮箱
        if get_auth_value("OAUTH_SYNC_EMAIL") and email and user.email != email:
            update_data["email"] = email
            
        # 同步头像 (只有成功获取到新头像才更新)
        if get_auth_value("OAUTH_SYNC_AVATAR") and avatar:
             update_data["avatar"] = avatar
            
        if update_data:
            try:
                # useroper 没有 update 方法，直接使用 user 实体的 update 方法
                # update 方法由 Base 类提供，被 @db_update 装饰器包装
                user.update(useroper._db, update_data)
                logger.info(f"OAuth 用户 {username} 更新资料: {list(update_data.keys())}")
                # 重新查询用户以获取最新数据
                user = useroper.get_by_name(username)
            except Exception as e:
                logger.warning(f"同步用户 {username} 资料失败: {e}")
    
    if not user.is_active:
        raise HTTPException(status_code=403, detail=f"用户 {username} 已被禁用")
    
    # 生成 MoviePilot JWT Token
    level = SitesHelper().auth_level
    # 是否显示配置向导
    show_wizard = not SystemConfigOper().get(SystemConfigKey.SetupWizardState) and not settings.ADVANCED_MODE
    
    jwt_token = create_access_token(
        userid=user.id,
        username=user.name,
        super_user=user.is_superuser,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        level=level
    )
    
    logger.info(f"用户 {username} 通过 OAuth2 认证成功")
    
    return schemas.Token(
        access_token=jwt_token,
        token_type="bearer",
        super_user=user.is_superuser,
        user_id=user.id,
        user_name=user.name,
        avatar=user.avatar,
        level=level,
        permissions=user.permissions or {},
        wizard=show_wizard
    )


@router.get("/enabled", summary="检查 OAuth 是否启用")
def oauth_enabled() -> dict[str, Any]:
    """检查 OAuth 是否启用并返回提供者信息"""
    if not get_auth_value("OAUTH_ENABLE"):
        return {
            "enabled": False,
            "provider": None
        }
    
    try:
        validate_oauth_config()
        return {
            "enabled": True,
            "provider": {
                "name": get_auth_value("OAUTH_PROVIDER_NAME"),
                "type": get_auth_value("OAUTH_PROVIDER_TYPE")
            }
        }
    except HTTPException:
        return {
            "enabled": False,
            "provider": None
        }
