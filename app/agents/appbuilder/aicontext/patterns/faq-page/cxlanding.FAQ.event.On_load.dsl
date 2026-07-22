FUNCTION On_load
    LOGIC
        setStore1_Copy_7: UIEngine.SetStore(path = "Page.id", value = "undefined")
        setStore: UIEngine.SetStore(path = "Page.concept", value = [{
    "Question": "What is Fincity Investment?",
    "Answer": "Fincity Investment is an online technology platform that provides users access to a curated set of real estate investment options and enables them to invest in any of the investments basis their selection."
}, {
    "Question": "What is fractional ownership or fractional investing?",
    "Answer": "Fractional ownership is when multiple investors come together to invest capital in an asset (which could be real estate, airplane, art etc.). It provides investors a percentage ownership in an asset, which gives proportionate rights in the income and capital value appreciation of the asset. It is a simple way to own an expensive asset, by splitting the ownership. Fractional ownership of real estate splits the ownership of high value property into smaller fractions to provide alternative investment avenues to retail investors along with proportionate ownership rights in the asset. It\u2019s a traditional concept and the simplest example of this is owning shares in a company, through which you have a fractional ownership in the company."
}, {
    "Question": "Are these investments through Fractional Ownership in Commercial Property secured?",
    "Answer": "Yes, all the investments are fully secured by underlying real estate. Such investments offer a fractional ownership in the underlying real estate. The real estate can be office, retail, warehousing, data center or residential assets. Such assets could either be operational (stabilized through a long-term lease) or under construction. Comprehensive details are available under the respective opportunity sections."
}, {
    "Question": "Are all fractional commercial real estate investment opportunities backed by real estate assets?",
    "Answer": "Yes. All fractional commercial real estate investment opportunities showcased on the platform, have real estate as the underlying asset through ownership. We believe that real estate is one of the most tangible asset class offering adequate security."
}, {
    "Question": "How is fractional investing on the platform different than REITs?",
    "Answer": "REITs, or real estate investment trusts is a trust that owns various income producing real estate. The REIT Manager has discretion to manage these assets on behalf of the REIT unitholders or investors. As per regulations, it is required to invest majority of the capital in completed assets and distribute at least 90% of the distributable income from these assets. The key differences between REIT and fractional investing are: 1. Fractional investing involves investing in a particular asset in a particular micro-market with a particular risk profile. On the other hand, REITs are diversified across a pool of assets across geographies. A simple way to understand this difference is investing in a particular stock versus investing in a sector specific mutual fund. Investors have their own views on each type basis individual specific risk appetite and invest across both basis their risk profiles. 2. In fractional investing, you can choose your investments across assets or geography. In REITs, the REIT manager has discretion to manage the investments through buying or selling assets. This makes fractional investing more customizable in terms of portfolio creation and diversification across asset classes. At present REITs in India are more asset class specific. 3. REITs, listed on stock exchanges, are more volatile in terms of the price movements. On the other hand, fractional investing is less volatile when compared to REITs. REITs have a lower investment amount and hence are more liquid as they trade on the stock exchanges. 4. Fractional investments are more illiquid, but our platform aggregates demand to facilitate secondary transfers of fractional investments. 5. At present, REITs are more asset class specific and only office REITs are available. On the other hand, fractional investing allows investors to customize their real estate portfolio from a more diverse pool of investments across asset class like office, warehousing and other emerging asset classes."
}])
        setStore_Copy_1: UIEngine.SetStore(path = "Page.investmentStructure", value = [{
    "Question": "Who can invest?",
    "Answer": "Resident Indian, Non Resident Indian (NRI) and Overseas Citizen of India (OCI)."
}, {
    "Question": "How are the investments structured?",
    "Answer": "Each investment is held in a special purpose vehicle (SPV), which will be a private limited company. In a private limited company, the Investors are issued shares and debentures of the SPV holding the asset, which represents their investment in the SPV."
}, {
    "Question": "Is the property registered in my name?",
    "Answer": "No, the property or asset is registered in a separate SPV setup specifically for the purpose of acquisition and holding the particular asset."
}, {
    "Question": "How do I obtain a title interest (ownership) of the property?",
    "Answer": "As mentioned above, the title to the property is in the name of the SPV. In terms of your fractional ownership, you will be issued ownership in the specific SPV holding the property, proportionate to the amount invested. The proportionate ownership in the SPV is represented in the form of securities issued to you. The above structure enables a user to get fractional ownership of the property."
}, {
    "Question": "Does the structure of holding the property in a SPV in any way legally impact my interest in the property?",
    "Answer": "No. Many companies and individuals have historically and continue to hold properties through property specific SPVs. Investment in a property through the SPV route for property ownership provides the same interest in the underlying property as direct ownership of property and does not legally impact any title interest in the property."
}, {
    "Question": "What are the instruments through which my investment will be made?",
    "Answer": "Investment is made through subscription of shares and debentures in the SPV in compliance with applicable laws."
}, {
    "Question": "What are the documents that are executed by the investor?",
    "Answer": "Term sheet, subscription agreements and other related agreements will be executed."
}, {
    "Question": "Is there any liability on the investor as a shareholder, for any act of the Pvt Ltd company?",
    "Answer": "No."
}])
        setStore_Copy_2: UIEngine.SetStore(path = "Page.investmentProcess", value = [{
    "Question": "Is the entire investment process digital?",
    "Answer": "Yes. Fincity Investment strives to make property investment as simple, seamless and transparent as investing in stocks. Hence the entire investment process for the investor is paperless right from KYC compliances, accessing property diligence reports and execution of transaction documents. We ensure that all investment information and diligence reports are made available upfront to enable your investment decision. Hence the investment process is completed in a few days rather than the traditional real estate investment process which takes a few months."
}, {
    "Question": "How much is the token amount for the investment?",
    "Answer": "The token amount is 10% of your total investment amount."
}, {
    "Question": "How are my funds secured while the investment is under offer/subscription?",
    "Answer": "Until the investment opportunity is fully subscribed, your funds are kept in a separate escrow account and will be held in trust. These funds are completely ringfenced from the operations of the Company and are safely kept in a legal escrow operated by a Trustee."
}, {
    "Question": "When does the registration happen?",
    "Answer": "The registration of the property in the SPV is done as soon as the investment opportunity is fully subscribed and will be completed within the window period starting from the day the opportunity goes live for investment. The window period is mentioned under each opportunity and is around 60-90 days."
}, {
    "Question": "Will I have to go for the property registration, in case of fractional ownership?",
    "Answer": "No, as the property is being registered in an SPV, you will not be required to go for the registration. Authorised representatives of the SPV will complete the property registration compliances."
}, {
    "Question": "What happens if the opportunity is not fully subscribed?",
    "Answer": "In such a case, we refund the entire investment amount made till date back to the investor."
}, {
    "Question": "Is my payment refundable?",
    "Answer": "No. Any payment made towards an investment opportunity, which gets fully subscribed, is non-refundable."
}, {
    "Question": "Can I take a loan to finance a portion of my investment amount?",
    "Answer": "As of now there are no loans that can be taken to finance a portion of the investment amount."
}])
        setStore_Copy_3: UIEngine.SetStore(path = "Page.investmentReturns", value = [{
    "Question": "Are the returns guaranteed? What are the risks involved?",
    "Answer": "No. The returns are not guaranteed. All returns are projections and are subject to changes in macroeconomic conditions, tenant default, vacancy, leasing risk, changing real estate market conditions, development risk, change in real estate regulations, title/litigation risk, approval risk, cash flow risk, liquidity risk, default risk etc. We advise investors to undertake their independent assessment (or through their counsels) on the attractiveness of any opportunity, vetting of diligence reports and independent assessment of projected returns. Fincity Investment will never recommend/advise/solicit any investment. The company is merely providing a platform to showcase curated real estate investment opportunities."
}, {
    "Question": "Are the returns shown post-tax?",
    "Answer": "No. All returns, including yield and returns, are shown on pre-tax and on gross basis."
}])
        setStore_Copy_4: UIEngine.SetStore(path = "Page.assetPortfolio", value = [{
    "Question": "What is the scope of property services offered by the company to the investors, in case of fractional ownership?",
    "Answer": "The company or its affiliates provide certain property services in relation to real estate asset held in the SPV. The scope of the property services is summarized below:\n1. Co-ordinating the property management (through 3rd party property managers) including management of lease, refurbishment (if required) and other activities that may be required for upkeep of property (like property tax payments). Actual costs for point 1 and towards brokerage, refurbishment and property tax will be charged to the SPV.\n2. Negotiations and management efforts with existing lessee.\n3. SPV compliances, audits and tax filings. (not including applicable taxes in SPV)\n4. Co-ordination efforts for rent transfers from lessees to SPV and then to investors."
}, {
    "Question": "What does the property services fee of 1% for fractional ownership charged on the Capital invested cover?",
    "Answer": "The property services fee charged covers cost for SPV\u2019s compliances such as tax filings and audit expenses, rent transfers, use of the technology platform for portfolio management and property updates, management costs for coordination efforts with existing lessees. The property services fee is only being charged for commercial property investment opportunities listed on the platform."
}, {
    "Question": "What is the scope of the Portfolio management by the company?",
    "Answer": "Each user has access to a digital portfolio dashboard attached to their user account which provides information regarding the performance of their investments including distributions, returns details and property updates. Investor voting required for key decisions regarding the property is also done through the Portfolio section."
}, {
    "Question": "What happens if a lessee moves out and property becomes vacant?",
    "Answer": "In such an event, there would be no rental income/ distributions to the investors during the period the property is vacant. The company\u2019s asset management team works with occupiers, brokers and operators to find a suitable tenant for the property."
}, {
    "Question": "What is the concept of voting by investors and how does the process run?",
    "Answer": "Certain key decisions to be undertaken by the SPV like re-leasing, property sale or property refurbishments are carried out based on decisions taken through voting by the investors in the particular investment. A decision is taken basis a simple majority concept, unless a higher threshold is prescribed under applicable laws."
}, {
    "Question": "How do I exit my investment, in case of fractional ownership?",
    "Answer": "You can list your investment for resale through the secondary market on the platform where we find suitable bids against your offer for sale. Alternately in the case of fractional ownership, after a certain number of years, the investors can vote to sell the SPV or the asset to another financial investor or operator."
}, {
    "Question": "Can I sell my investment to a 3rd party through my own network?",
    "Answer": "Yes. In such an event, the user may contact the relationship manager to help facilitate the transfer."
}, {
    "Question": "What happens if the platform ceases to operate?",
    "Answer": "The assets are held in specific SPVs which are separate distinct entities from the company or the tech platform. There would be no impact to the specific SPVs if the platform ceases to operate. In an event that the platform ceases to operate, the Company would cease to provide asset management services. In such a scenario, the Company will assist the investors in engaging a third-party entity to provide such services to ensure continuity and sustained operations of the SPVs."
}])
        setStore_Copy_5: UIEngine.SetStore(path = "Page.servicesFees", value = [{
    "Question": "What are the fees that are payable by investors?",
    "Answer": "We charge the following fees:\n1. One time Acquisition fee of up to 3% on Capital invested at the time of investment. This forms a part of the property acquisition cost. This is towards the costs for deal origination, research, brokerage fees and deals screening. Only a few deals pass through our stringent underwriting standards, which are eventually then offered to you on the platform.\n2. Property Services fee of 1% per annum on Capital invested payable monthly.\n3. Performance fee, which is a percentage of the upside over the investor reaching a hurdle IRR. Please refer to respective investment opportunity details on the website."
}])
        setStore_Copy_6: UIEngine.SetStore(path = "Page.taxation", value = [{
    "Question": "Is TDS deducted on the distributions?",
    "Answer": "Yes, TDS is deducted as per applicable laws."
}, {
    "Question": "How are the distributions happening from SPV to the investor, in case of fractional ownership?",
    "Answer": "The investors receive distributions from the SPV in the form of monthly interest on the securities (debentures) held by investors in such SPV. TDS certificates will be provided to you on a quarterly or semi-annual basis."
}, {
    "Question": "What is the tax payable by investors on the distributions received?",
    "Answer": "The distributions are taxable in the hands of the users as per the user\u2019s respective tax bracket. Further, distributions would also be subject to a deduction of tax (TDS) at a rate of 10% before remitting to the user. A TDS certificate will be provided for any tax deducted at source on a quarterly or semi-annual basis and such TDS would be available as a credit to the Investor. The tax implications provided herein are provided on an indicative basis and users are advised to consult their respective tax experts with respect to any tax related matters."
}, {
    "Question": "How are exits from investments taxed?",
    "Answer": "Any amount received by an investor through exit of investment would be taxable as capital gains in the hands of the investor. If the investment in equity shares is held by the Investor for a period greater than 24 months, such capital gains would be treated as long term in nature and taxable at 20% (plus surcharge and cess), subject to indexation. If the investment is held by the Investor for a period less than 24 months, such capital gains would be treated as short term in nature and taxable as per the user\u2019s respective tax bracket. If the investment in debentures is held by the Investor for a period greater than 36 months, such capital gains would be treated as long term in nature and taxable at 20% (plus surcharge and cess). If the investment is held by the Investor for a period less than 36 months, such capital gains would be treated as short term in nature and taxable as per the user\u2019s respective tax bracket."
}])
        setStore1: UIEngine.SetStore(path = "Page.sections", value = [{
    "name": "concept",
    "heading": "Concept & Regularity"
}, {
    "name": "investmentStructure",
    "heading": "Investment Structure"
}, {
    "name": "investmentProcess",
    "heading": "Investment Process"
}, {
    "name": "investmentReturns",
    "heading": "Investment Returns"
}, {
    "name": "assetPortfolio",
    "heading": "Asset Portfolio"
}, {
    "name": "servicesFees",
    "heading": "Services Fees"
}, {
    "name": "taxation",
    "heading": "Taxation"
}])
        onload2: _.onload2()
        setStore1_Copy_1: UIEngine.SetStore(path = "Page.popup", value = false)