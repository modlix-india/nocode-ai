"""Authentication models matching Java ContextAuthentication"""
from pydantic import BaseModel, Field
from typing import Optional, List


class ContextUser(BaseModel):
    """User details from security service"""
    id: Optional[int] = None
    userName: Optional[str] = None
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    emailId: Optional[str] = None
    phoneNumber: Optional[str] = None
    clientId: Optional[int] = None
    clientCode: Optional[str] = None
    stringAuthorities: Optional[List[str]] = None
    
    class Config:
        extra = "ignore"


class ContextAuthentication(BaseModel):
    """Authentication context from security service"""
    user: Optional[ContextUser] = None
    # Java serializes "isAuthenticated" as "authenticated" (JavaBeans convention)
    isAuthenticated: bool = Field(default=False, alias="authenticated")
    loggedInFromClientId: Optional[int] = None
    loggedInFromClientCode: Optional[str] = None
    clientTypeCode: Optional[str] = None
    clientLevelType: Optional[str] = None
    clientCode: Optional[str] = None
    accessToken: Optional[str] = None
    urlClientCode: Optional[str] = None
    urlAppCode: Optional[str] = None
    verifiedAppCode: Optional[str] = None
    
    class Config:
        extra = "ignore"
        populate_by_name = True  # Accept both "authenticated" and "isAuthenticated"

