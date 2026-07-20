FUNCTION Page_on_load
    LOGIC
        setStore9: UIEngine.SetStore(path = "Page.fileSel", value = {
    "id": 19491,
    "name": "image (1).png",
    "size": 552914,
    "filePath": "/image (1).png",
    "url": "api/files/static/file/DYPIV/image+%281%29.png",
    "createdDate": 1751975124,
    "lastModifiedTime": 1751975124,
    "type": "png",
    "fileName": "image (1)",
    "directory": false,
    "isCompressedFile": false
})
            output
                placement_previews: UIEngine.SetStore(path = "Page.placementPreviews", value = {
    "facebook": {
        "feed": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/feeds.jpg",
            "preview_name": "Facebook Feed"
        },
        "instream_video": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/FacebookInstreamVideos.png",
            "preview_name": "Facebook in-stream videos"
        },
        "right_hand_column": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/FacebookRightColumn.jpg",
            "preview_name": "Facebook right column"
        },
        "marketplace": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/FacebookMarketplace.jpg",
            "preview_name": "Facebook Marketplace"
        },
        "search": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/FacebookSearchResults.png",
            "preview_name": "Facebook search results"
        },
        "facebook_reels": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/FacebookReels.png",
            "preview_name": "Facebook Reels"
        },
        "story": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/InstagramStories.png",
            "preview_name": "Facebook Story"
        },
        "biz_disco_feed": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/InstagramStories.png",
            "preview_name": "Facebook Bizdisco Feed"
        },
        "facebook_reels_overlay": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/InstagramStories.png",
            "preview_name": "Facebook Stories"
        },
        "profile_feed": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/FacebookProfileFeed.png",
            "preview_name": "Facebook profile feed"
        },
        "notification": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/FacebookNotifications.png",
            "preview_name": "Facebook notifications"
        },
        "video_feeds": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/FacebookVideoFeeds.jpg",
            "preview_name": "Facebook video feeds"
        }
    },
    "instagram": {
        "stream": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/FacebookVideoFeeds.jpg",
            "preview_name": "Instagram Stream"
        },
        "explore": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/InstagramExplore.png",
            "preview_name": "Instagram Explore"
        },
        "story": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/InstagramExplore.png",
            "preview_name": "Instagram Stories"
        },
        "reels": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/InstagramReels.png",
            "preview_name": "Instagram Reels"
        },
        "explore_home": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/InstagramExploreHome.png",
            "preview_name": "Instagram Expolore home"
        },
        "profile_feed": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/InstagramProfileFeed.png",
            "preview_name": "Instagram profile feed"
        },
        "ig_search": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/InstagramSearchResults.png",
            "preview_name": "Instagram search results"
        }
    },
    "messenger": {
        "messenger_home": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/MessengerSponsoredMessages.png",
            "preview_name": "Messenger home"
        },
        "story": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/MessengerStories.png",
            "preview_name": "Messenger Stories"
        }
    },
    "audience_network": {
        "classic": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/MessengerStories.png",
            "preview_name": "Messenger Stories"
        },
        "instream_video": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/MessengerStories.png",
            "preview_name": "Messenger Stories"
        },
        "rewarded_video": {
            "image_url": "api/files/static/file/SYSTEM/MarketingAI/Targeting Placements Preview/AudienceNetworkRewardVideos.png",
            "preview_name": "Audience Network rewarded videos"
        }
    }
}) AFTER Steps.setStore9.output
                    output
                        setStore1: UIEngine.SetStore(path = "Page.userEnterLocations", value = []) AFTER Steps.placement_previews.output
                            output
                                excluded_geo_locations: UIEngine.SetStore(value = [], path = "Page.excluded_geo_locations_array") AFTER Steps.setStore1.output
                                    output
                                        setStore: UIEngine.SetStore(path = "Page.targetingType", value = "Include") AFTER Steps.excluded_geo_locations.output
                                            output
                                                setingIndexNum: UIEngine.SetStore(path = "Page.indexNum", value = "") AFTER Steps.setStore.output
                                                    output
                                                        setStore2: UIEngine.SetStore(path = "Page.exclude_loc_index_num", value = "") AFTER Steps.setingIndexNum.output
                                                            output
                                                                setStore3: UIEngine.SetStore(path = "Page.ShowSuggestionsGrid", value = false) AFTER Steps.setStore2.output
                                                                    output
                                                                        setStore4: UIEngine.SetStore(path = "Page.classType", value = "interests") AFTER Steps.setStore3.output
                                                                            output
                                                                                setStore6: UIEngine.SetStore(path = "Page.relevanceInterestList", value = []) AFTER Steps.setStore4.output
                                                                                    output
                                                                                        fetchInteretsDemographisBehaviors: _.FetchInteretsDemographisBehaviors() AFTER Steps.setStore6.output
                                                                                            output
                                                                                                setStore5: UIEngine.SetStore(path = "Page.InterstTypes", value = {}) AFTER Steps.fetchInteretsDemographisBehaviors.output
                                                                                                    output
                                                                                                        setStore7: UIEngine.SetStore(path = "Page.selectPlacements", value = false) AFTER Steps.setStore5.output
                                                                                                            output
                                                                                                                setStore8: UIEngine.SetStore(path = "Page.placementControls", value = false) AFTER Steps.setStore7.output
                                                                                                                    output
                                                                                                                        new_Function_1: _.New_Function_1() AFTER Steps.setStore8.output