"""GraphQL feature-flag sets for the various x.com endpoints."""

from __future__ import annotations

LIST_LATEST_TWEETS_TIMELINE = {
    "rweb_video_screen_enabled": False,
    "rweb_cashtags_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "rweb_cashtags_composer_attachment_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

# BlueVerifiedFollowers uses the same feature set as LIST_LATEST_TWEETS_TIMELINE.
BLUE_VERIFIED_FOLLOWERS = dict(LIST_LATEST_TWEETS_TIMELINE)

# Regular Followers use the same feature set too.
FOLLOWERS = dict(LIST_LATEST_TWEETS_TIMELINE)

# HomeTimeline = LIST_LATEST_TWEETS_TIMELINE + one extra flag.
HOME_TIMELINE = {
    **LIST_LATEST_TWEETS_TIMELINE,
    "rweb_conversational_replies_downvote_enabled": False,
}

# SearchTimeline uses the same feature set as HomeTimeline (verified via DevTools).
SEARCH_TIMELINE = dict(HOME_TIMELINE)

# CreateTweet (mutation): the broad timeline set + composer-specific flags. X
# ignores extra features; a missing required one returns an error naming it, so a
# superset is the safer choice.
CREATE_TWEET = {
    **HOME_TIMELINE,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
}

USER_BY_SCREEN_NAME = {
    "responsive_web_grok_bio_auto_translation_is_enabled": False,
    "hidden_profile_subscriptions_enabled": True,
    "payments_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}

USER_BY_SCREEN_NAME_FIELD_TOGGLES = {
    "withAuxiliaryUserLabels": True,
}
