from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.sitemaps.views import sitemap
from django.urls import path, reverse_lazy
from portal import views
from portal.sitemaps import PublicPagesSitemap

sitemaps = {'public': PublicPagesSitemap()}

urlpatterns = [
    path('robots.txt', views.robots_txt, name='robots-txt'),
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='sitemap'),
    path('admin/initial-setup/', views.admin_initial_setup, name='admin-initial-setup'),
    path('admin/users/', views.admin_users, name='admin-users'),
    path('admin/users/create/', views.admin_create_account, name='admin-create-account'),
    path('admin/users/<int:user_id>/toggle-active/', views.admin_toggle_user_active, name='admin-toggle-user-active'),
    path('admin/', admin.site.urls),
    path('', views.login_user, name='home'),
    path('login/', views.login_user, name='login'),
    path('otp/send/', views.send_phone_otp, name='send-phone-otp'),
    path('otp/email/send/', views.send_email_otp, name='send-email-otp'),
    path(
        'password-reset/',
        auth_views.PasswordResetView.as_view(
            form_class=views.ResilientPasswordResetForm,
            template_name='password_reset.html',
            email_template_name='password_reset_email.txt',
            subject_template_name='password_reset_subject.txt',
            success_url=reverse_lazy('password-reset-done'),
        ),
        name='password-reset',
    ),
    path('password-reset/done/', auth_views.PasswordResetDoneView.as_view(template_name='password_reset_done.html'), name='password-reset-done'),
    path(
        'password-reset/confirm/<uidb64>/<token>/',
        auth_views.PasswordResetConfirmView.as_view(
            template_name='password_reset_confirm.html',
            success_url=reverse_lazy('password-reset-complete'),
        ),
        name='password-reset-confirm',
    ),
    path('password-reset/complete/', auth_views.PasswordResetCompleteView.as_view(template_name='password_reset_complete.html'), name='password-reset-complete'),
    path('register/', views.register_account, name='register'),
    path('selector/apply/', views.apply_as_selector, name='selector-apply'),
    path('feed/', views.feed, name='feed'),
    path('feed/post/<int:post_id>/like/', views.like_post, name='like-post'),
    path('feed/post/<int:post_id>/comment/', views.comment_post, name='comment-post'),
    path('feed/post/<int:post_id>/delete/', views.delete_post, name='delete-post'),
    path('players/', views.people, name='people'),
    path('search/', views.global_search, name='global-search'),
    path('chat/', views.chat_inbox, name='chat-inbox'),
    path('chat/<int:friend_id>/', views.chat_thread, name='chat-thread'),
    path('chat/<int:friend_id>/updates/', views.chat_updates, name='chat-updates'),
    path('profile/', views.player_profile, name='player-profile'),
    path('records/', views.records, name='records'),
    path('records/athlete/<int:athlete_id>/', views.athlete_record, name='athlete-record'),
    path('ai/insight/', views.gemini_insight, name='gemini-insight'),
    path('ai/question/', views.gemini_question, name='gemini-question'),
    path('secure-media/<path:path>', views.protected_media, name='protected-media'),
    path('logout/', views.logout_user, name='logout'),
    path('dashboard/', views.dashboard, name='dashboard'),
]