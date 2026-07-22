FUNCTION Checking_Location_Targeting_Type
    LOGIC
        checking_targeting_type: System.If(condition = `Page.targetingType = "Include"`)
            true
                user_enter_locations_optimized: _.user_enter_locations_optimized() AFTER Steps.checking_targeting_type.true
            false
                user_excluded_geo_locations: _.user_excluded_geo_locations() AFTER Steps.checking_targeting_type.false