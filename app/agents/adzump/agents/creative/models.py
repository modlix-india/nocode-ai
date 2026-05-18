from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field

PrimaryTextStr = Annotated[str, Field(max_length=125)]
HeadlineStr = Annotated[str, Field(max_length=40)]
DescriptionStr = Annotated[str, Field(max_length=30)]


class CallToAction(str, Enum):
    APPLY_NOW = "APPLY_NOW"
    BOOK_NOW = "BOOK_NOW"
    BUY_TICKETS = "BUY_TICKETS"
    CONTACT_US = "CONTACT_US"
    DOWNLOAD = "DOWNLOAD"
    GET_OFFER = "GET_OFFER"
    GET_QUOTE = "GET_QUOTE"
    GET_SHOWTIMES = "GET_SHOWTIMES"
    LEARN_MORE = "LEARN_MORE"
    LISTEN_NOW = "LISTEN_NOW"
    ORDER_NOW = "ORDER_NOW"
    PLAY_GAME = "PLAY_GAME"
    REQUEST_TIME = "REQUEST_TIME"
    SEE_MENU = "SEE_MENU"
    SHOP_NOW = "SHOP_NOW"
    SIGN_UP = "SIGN_UP"
    SUBSCRIBE = "SUBSCRIBE"
    WATCH_MORE = "WATCH_MORE"


class CreativeText(BaseModel):
    primary_texts: list[PrimaryTextStr] = Field(..., min_length=5, max_length=5)

    headlines: list[HeadlineStr] = Field(..., min_length=5, max_length=5)

    descriptions: list[DescriptionStr] = Field(..., min_length=5, max_length=5)

    cta: CallToAction
