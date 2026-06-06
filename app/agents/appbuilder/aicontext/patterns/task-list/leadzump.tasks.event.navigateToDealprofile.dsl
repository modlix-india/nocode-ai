FUNCTION navigateToDealprofile
    LOGIC
        hasAuthority: CoreServices.SecurityContext.HasAuthority(authority = `'Authorities.LEADZUMP.ROLE_Deal_READ'`)
            output
                if: System.If(condition = Steps.hasAuthority.output.result)
                    true
                        navigatingToDealProfile: UIEngine.Navigate(linkPath = `'/dealProfile/{{Parent.ticketId.code}}'`, force = true, target = "_blank") AFTER Steps.if.true